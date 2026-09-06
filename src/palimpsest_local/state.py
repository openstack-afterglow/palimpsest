"""Owner-only XDG state, atomic ledgers, tags, locks, and transfer checkpoints."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat as stat_module
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .digest import normalize_digest
from .errors import StateError
from .runtime_types import (
    ALLOWED_RUNTIME_STATUSES,
    DispatchKey,
    ExistingRunRecord,
    LogErrorCategory,
    LogStreamError,
    RunAggregationError,
    RuntimeBackend,
    RuntimeKind,
    RuntimeOperation,
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9.+\-]{0,63}$")
_STATUSES = {"creating", "defined", "starting", "running", "stopping", "stopped", "removed", "failed"}
_MAX_RUN_LEDGER_BYTES = 1024 * 1024
_MAX_LIFECYCLE_REVISION = 2**63 - 1


@dataclass(frozen=True, slots=True)
class RunLedgerSnapshot:
    """Internal pinned ledger payload; callers must project before exposure."""

    record: ExistingRunRecord
    state: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class StatePaths:
    config: Path
    state: Path

    @property
    def store(self) -> Path:
        return self.state / "store"

    @property
    def runs(self) -> Path:
        return self.state / "runs"

    @property
    def run_deletions(self) -> Path:
        """Quarantined run trees whose committed deletion cleanup did not finish."""
        return self.state / "run-deletions"

    @property
    def locks(self) -> Path:
        return self.state / "locks"

    @property
    def transfers(self) -> Path:
        return self.state / "transfers"

    @property
    def tags(self) -> Path:
        return self.state / "tags"

    @property
    def builds(self) -> Path:
        return self.state / "builds"

    @property
    def build_cache(self) -> Path:
        """BuildKit cache scopes managed by Palimpsest.

        These bytes are an optimization, not an authority: imported cache archives
        are digest-verified and BuildKit revalidates their records before use.
        """
        return self.state / "build-cache"

    @property
    def runtime_packs(self) -> Path:
        """Conversion-cache index from a bound pack-policy key to SquashFS CAS bytes."""
        return self.state / "runtime-packs"

    @property
    def oci_derived_store(self) -> Path:
        """Private v1 OCI-derived indexes, separate from BuildKit's flat schema."""
        return self.runtime_packs / "oci-derived-v1"

    @property
    def oci_source_cas(self) -> Path:
        """Private descriptor-verified source blobs for local OCI intake."""
        return self.runtime_packs / "oci-source-v1"

    @property
    def projects(self) -> Path:
        """Declarative ``palimpsest.yml`` project ledgers."""
        return self.state / "projects"

    @property
    def volumes(self) -> Path:
        """Project-owned writable block-volume artifacts."""
        return self.state / "volumes"

    @property
    def oci_root_volumes(self) -> Path:
        """Independently retained OCI-root writable block volumes."""
        return self.state / "oci-root-volumes"


@dataclass(frozen=True)
class RunPaths:
    root: Path
    owner: Path
    state: Path
    overlay: Path
    seed: Path
    console: Path
    ssh: Path
    identity: Path
    identity_public: Path
    known_hosts: Path
    lock: Path
    name: str


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    state: Path
    volumes: Path
    lock: Path
    name: str


@dataclass(frozen=True)
class OwnerRecord:
    schema_version: int
    run_id: str
    name: str


class NewRunReservation:
    """Pinned exclusive authority for one newly-created run ledger.

    Palimpsest writers sharing this state root must honor the per-name lock.
    Descriptor-relative publication protects the reservation from accidental
    path re-resolution; it is not an OS sandbox against arbitrary same-UID
    processes that deliberately rewrite owner-controlled storage.
    """

    __slots__ = (
        "_roots",
        "_paths",
        "_record",
        "_dispatch_key",
        "_runs_fd",
        "_run_fd",
        "_locks_fd",
        "_lock_fd",
        "_runs_identity",
        "_run_identity",
        "_locks_identity",
        "_lock_identity",
        "_last_status",
        "_lifecycle_revision",
    )

    def __init__(
        self,
        roots: StatePaths,
        paths: RunPaths,
        record: OwnerRecord,
        dispatch_key: DispatchKey,
        runs_fd: int,
        run_fd: int,
        locks_fd: int,
        lock_fd: int,
        runs_identity: tuple[int, int],
        run_identity: tuple[int, int],
        locks_identity: tuple[int, int],
        lock_identity: tuple[int, int],
    ) -> None:
        object.__setattr__(self, "_roots", roots)
        object.__setattr__(self, "_paths", paths)
        object.__setattr__(self, "_record", record)
        object.__setattr__(self, "_dispatch_key", dispatch_key)
        object.__setattr__(self, "_runs_fd", runs_fd)
        object.__setattr__(self, "_run_fd", run_fd)
        object.__setattr__(self, "_locks_fd", locks_fd)
        object.__setattr__(self, "_lock_fd", lock_fd)
        object.__setattr__(self, "_runs_identity", runs_identity)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_locks_identity", locks_identity)
        object.__setattr__(self, "_lock_identity", lock_identity)
        object.__setattr__(self, "_last_status", None)
        object.__setattr__(self, "_lifecycle_revision", 0)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("new run reservation authority is immutable")

    @property
    def roots(self) -> StatePaths:
        return self._roots

    @property
    def paths(self) -> RunPaths:
        return self._paths

    @property
    def record(self) -> OwnerRecord:
        return self._record

    @property
    def dispatch_key(self) -> DispatchKey:
        return self._dispatch_key

    @property
    def last_status(self) -> str | None:
        return self._last_status

    @property
    def lifecycle_revision(self) -> int:
        return self._lifecycle_revision

    def verify_binding(self) -> None:
        _verify_new_run_binding(self)

    def write_state(self, status: str, data: Mapping[str, Any]) -> dict[str, Any]:
        with artifact_reference_guard(self.roots):
            payload = _write_reserved_run_state(self, status, data)
        object.__setattr__(self, "_last_status", status)
        object.__setattr__(self, "_lifecycle_revision", payload["lifecycle_revision"])
        return payload

    def write_failure(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Publish failure through the pinned directory even after name-path loss."""
        with artifact_reference_guard(self.roots):
            payload = _write_reserved_run_state(self, "failed", data, require_visible_binding=False)
        object.__setattr__(self, "_last_status", "failed")
        object.__setattr__(self, "_lifecycle_revision", payload["lifecycle_revision"])
        return payload

    def write_file(self, relative_name: str, content: bytes, *, mode: int = 0o600) -> Path:
        """Atomically publish one create-time file relative to the pinned run."""
        _write_reserved_run_file(self, relative_name, content, mode=mode)
        return self.paths.root.joinpath(*relative_name.split("/"))

    def publish_staging(self, staging_root: Path) -> None:
        """Move a fully generated, synced artifact tree into the pinned run."""
        _publish_reserved_run_staging(self, staging_root)


@dataclass(frozen=True)
class TagRecord:
    schema_version: int
    tag: str
    digest: str
    media_type: str
    size_bytes: int
    parent_digest: str | None
    base_image_digest: str | None
    source: str
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tag", validate_tag(self.tag))
        object.__setattr__(self, "digest", normalize_digest(self.digest))
        if self.parent_digest is not None:
            object.__setattr__(self, "parent_digest", normalize_digest(self.parent_digest))
        if self.base_image_digest is not None:
            object.__setattr__(self, "base_image_digest", normalize_digest(self.base_image_digest))


@dataclass(frozen=True)
class TransferRecord:
    schema_version: int
    digest: str
    path_fingerprint: str
    session_id: str
    acknowledged_offset: int
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "digest", normalize_digest(self.digest))


def _contained(root: Path, path: Path) -> Path:
    result = path.resolve()
    try:
        result.relative_to(root.resolve())
    except ValueError as exc:
        raise StateError(f"path escapes state root: {path}") from exc
    return result


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(word in key.lower() for word in ("token", "secret", "password", "private_key")):
                raise StateError("secret-shaped field is forbidden in state")
            _reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secrets(item)
    elif isinstance(value, str) and "-----BEGIN" in value and "KEY-----" in value:
        raise StateError("key-material-shaped string is forbidden in state")


def permission_bits(path: Path) -> int:
    return path.stat().st_mode & 0o777


def fsync_directory(path: Path) -> None:
    """Durably commit directory-entry changes or fail with an actionable error."""
    directory = path.expanduser().resolve()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError as exc:
        raise StateError(f"cannot open state directory for durability sync: {directory}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise StateError(f"cannot durably sync state directory: {directory}") from exc
    finally:
        os.close(fd)


@contextmanager
def pinned_owner_directory(path: Path, *, missing_ok: bool = False) -> Iterator[int | None]:
    """Pin one owner-private directory without following its final component."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise StateError("state directory authority must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
    fd: int | None = None
    try:
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            if missing_ok:
                yield None
                return
            raise StateError("state directory authority is missing") from None
        except OSError:
            raise StateError("state directory authority cannot be securely opened") from None
        opened = os.fstat(fd)
        visible = os.stat(path, follow_symlinks=False)
        if (
            not stat_module.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat_module.S_IMODE(opened.st_mode) & 0o022 != 0
            or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
        ):
            raise StateError("state directory authority is not owner-bound")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _toml_format_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    elif isinstance(val, (int, float)):
        return str(val)
    elif isinstance(val, str):
        return json.dumps(val)
    else:
        raise TypeError(f"Unsupported TOML value type: {type(val)}")


def _dump_simple_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for k, v in data.items():
        if not isinstance(v, dict):
            lines.append(f"{k} = {_toml_format_value(v)}")
    for k, v in data.items():
        if isinstance(v, dict) and v:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[{k}]")
            for sub_k, sub_v in v.items():
                if not isinstance(sub_v, dict):
                    lines.append(f"{sub_k} = {_toml_format_value(sub_v)}")
    return "\n".join(lines) + "\n"


def state_root_source(environment: dict[str, str] | None = None) -> str:
    env = environment if environment is not None else os.environ
    if env.get("PALIMPSEST_STATE_HOME"):
        return "env"
    config = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "palimpsest"
    config_file = config / "config.toml"
    if config_file.is_file():
        try:
            cfg_data = tomllib.loads(config_file.read_text(encoding="utf-8"))
            storage_table = cfg_data.get("storage")
            if isinstance(storage_table, dict) and "state_root" in storage_table:
                sr_val = storage_table["state_root"]
                if isinstance(sr_val, str) and sr_val.strip():
                    return "config"
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return "default"


def resolve_roots(environment: dict[str, str] | None = None) -> StatePaths:
    """Resolve configured state paths without creating or changing filesystem objects."""
    env = environment if environment is not None else os.environ
    config = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "palimpsest"

    env_state = env.get("PALIMPSEST_STATE_HOME")
    if env_state:
        state_path = Path(env_state)
        if not state_path.is_absolute():
            raise StateError("PALIMPSEST_STATE_HOME must be an absolute path")
        state = state_path
    else:
        config_file = config / "config.toml"
        cfg_state_root: Path | None = None
        if config_file.is_file():
            try:
                cfg_data = tomllib.loads(config_file.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise StateError(f"invalid storage.state_root in {config_file}") from exc
            storage_table = cfg_data.get("storage")
            if isinstance(storage_table, dict) and "state_root" in storage_table:
                sr_val = storage_table["state_root"]
                if isinstance(sr_val, str):
                    sr_path = Path(sr_val)
                    if not sr_path.is_absolute():
                        raise StateError(f"invalid storage.state_root in {config_file}")
                    cfg_state_root = sr_path
                else:
                    raise StateError(f"invalid storage.state_root in {config_file}")
        if cfg_state_root is not None:
            state = cfg_state_root
        else:
            state = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "palimpsest"

    return StatePaths(config, state)


def init_roots(environment: dict[str, str] | None = None) -> StatePaths:
    return init_resolved_roots(resolve_roots(environment))


def init_resolved_roots(roots: StatePaths) -> StatePaths:
    """Initialize exactly one previously resolved root authority without re-reading config."""

    if not isinstance(roots, StatePaths):
        raise TypeError("root initialization requires resolved StatePaths")
    # Lazy import: the shared registry also uses StatePaths. Its guard owns safe
    # state/runs/locks bootstrap and preserves authorized traversal ACLs.
    from .oci_shared_traversal import shared_traversal_initialization

    with shared_traversal_initialization(roots):
        protected = {
            _identity(path.stat(follow_symlinks=False))
            for path in (roots.state, roots.runs, roots.locks, roots.oci_root_volumes)
        }
        for directory in (
            roots.config,
            roots.store,
            roots.transfers,
            roots.tags,
            roots.builds,
            roots.build_cache,
            roots.runtime_packs,
            roots.oci_derived_store,
            roots.oci_source_cas,
            roots.projects,
            roots.volumes,
        ):
            _initialize_private_root(directory, protected)
    # This cleanup acquires run locks; never nest it under the shared lock.
    _retry_run_deletion_quarantines(roots)
    return roots


def _initialize_private_root(directory: Path, protected: set[tuple[int, int]]) -> None:
    """Keep private roots private without following aliases onto shared roots."""

    directory_fd: int | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        visible = directory.stat(follow_symlinks=False)
        if (
            not stat_module.S_ISDIR(visible.st_mode)
            or visible.st_uid != os.geteuid()
            or _identity(visible) in protected
        ):
            raise StateError("invalid private root directory")
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        held = os.fstat(directory_fd)
        if _identity(held) != _identity(visible):
            raise StateError("private root directory changed")
        os.fchmod(directory_fd, 0o700)
        current = directory.stat(follow_symlinks=False)
        if _identity(current) != _identity(held) or not stat_module.S_ISDIR(current.st_mode):
            raise StateError("private root directory changed")
    except OSError as exc:
        raise StateError("cannot initialize private root directory") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def write_state_root(roots: StatePaths, destination: Path) -> None:
    dest_path = Path(destination)
    if not dest_path.is_absolute():
        raise StateError("state root destination must be an absolute path")
    config_file = roots.config / "config.toml"
    cfg_data: dict[str, Any] = {}
    if config_file.is_file():
        try:
            cfg_data = tomllib.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            cfg_data = {}
    if "storage" not in cfg_data or not isinstance(cfg_data["storage"], dict):
        cfg_data["storage"] = {}
    cfg_data["storage"]["state_root"] = str(dest_path)

    content = _dump_simple_toml(cfg_data)
    roots.config.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(roots.config, 0o700)

    fd, temporary = tempfile.mkstemp(prefix=".config-tmp-", dir=roots.config)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_file)
        fsync_directory(roots.config)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def paths() -> StatePaths:
    return init_roots()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _reject_secrets(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise StateError("state file not found")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"invalid state JSON: {path}") from exc
    if not isinstance(value, dict):
        raise StateError("state JSON must be an object")
    return value


def run_paths(roots: StatePaths, name: str) -> RunPaths:
    if _NAME_RE.fullmatch(name) is None:
        raise StateError("invalid run name")
    root = _contained(roots.runs, roots.runs / name)
    return RunPaths(
        root,
        root / "owner.json",
        root / "state.json",
        root / "overlay.qcow2",
        root / "seed.iso",
        root / "console.log",
        root / "ssh",
        root / "ssh" / "id_ed25519",
        root / "ssh" / "id_ed25519.pub",
        root / "ssh" / "known_hosts",
        roots.locks / f"{name}.lock",
        name,
    )


def _new_run_paths(roots: StatePaths, name: str) -> RunPaths:
    """Build canonical-parent paths whose safety is enforced by dirfd reservation."""
    if _NAME_RE.fullmatch(name) is None:
        raise StateError("invalid run name")
    canonical_runs = roots.runs.resolve()
    canonical_locks = roots.locks.resolve()
    root = canonical_runs / name
    return RunPaths(
        root,
        root / "owner.json",
        root / "state.json",
        root / "overlay.qcow2",
        root / "seed.iso",
        root / "console.log",
        root / "ssh",
        root / "ssh" / "id_ed25519",
        root / "ssh" / "id_ed25519.pub",
        root / "ssh" / "known_hosts",
        canonical_locks / f"{name}.lock",
        name,
    )


def project_paths(roots: StatePaths, name: str) -> ProjectPaths:
    if _NAME_RE.fullmatch(name) is None:
        raise StateError("invalid project name")
    root = _contained(roots.projects, roots.projects / name)
    volumes = _contained(roots.volumes, roots.volumes / name)
    return ProjectPaths(
        root=root,
        state=root / "state.json",
        volumes=volumes,
        lock=roots.locks / f"project-{name}.lock",
        name=name,
    )


def write_owner_record(rpaths: RunPaths) -> OwnerRecord:
    if rpaths.owner.exists():
        raise StateError("owner record is immutable")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.ssh.mkdir(exist_ok=True, mode=0o700)
    record = OwnerRecord(1, str(uuid.uuid4()), rpaths.name)
    atomic_write_json(rpaths.owner, asdict(record))
    return record


def read_owner_record(rpaths: RunPaths) -> OwnerRecord:
    try:
        return OwnerRecord(**read_json(rpaths.owner))
    except TypeError as exc:
        raise StateError("invalid owner record") from exc


def _open_readonly_no_follow(
    path: str | Path,
    *,
    directory_fd: int | None = None,
    directory: bool = False,
    nonblocking: bool = False,
) -> int | None:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    if nonblocking:
        flags |= os.O_NONBLOCK
    try:
        return os.open(path, flags, dir_fd=directory_fd)
    except OSError:
        return None


def _safe_fstat(file_fd: int) -> os.stat_result | None:
    try:
        return os.fstat(file_fd)
    except OSError:
        return None


def _safe_stat(path: str | Path, *, directory_fd: int | None = None) -> os.stat_result | None:
    try:
        return os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None


def _close_noerror(file_fd: int) -> None:
    try:
        os.close(file_fd)
    except OSError:
        pass


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _write_all(file_fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(file_fd, content[offset:])
        except OSError:
            raise StateError("cannot durably write new run ledger") from None
        if written <= 0:
            raise StateError("cannot durably write new run ledger")
        offset += written


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    _reject_secrets(value)
    try:
        content = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    except (RecursionError, TypeError, ValueError):
        raise StateError("invalid new run ledger payload") from None
    if len(content) > _MAX_RUN_LEDGER_BYTES:
        raise StateError("new run ledger payload is too large")
    return content


def _read_exact_private_file(
    directory_fd: int,
    filename: str,
    *,
    max_bytes: int = _MAX_RUN_LEDGER_BYTES,
    expected_mode: int = 0o600,
) -> bytes:
    """Read one writer-owned file while proving inode and metadata stability."""
    if type(expected_mode) is not int or expected_mode not in {0o400, 0o600}:
        raise StateError("invalid private file verification mode")
    entry_before = _safe_stat(filename, directory_fd=directory_fd)
    if (
        entry_before is None
        or not stat_module.S_ISREG(entry_before.st_mode)
        or entry_before.st_uid != os.geteuid()
        or entry_before.st_nlink != 1
        or stat_module.S_IMODE(entry_before.st_mode) != expected_mode
        or entry_before.st_size > max_bytes
    ):
        raise StateError("run ledger changed during verification")
    file_fd = _open_readonly_no_follow(filename, directory_fd=directory_fd, nonblocking=True)
    if file_fd is None:
        raise StateError("run ledger changed during verification")
    try:
        opened_before = _safe_fstat(file_fd)
        if (
            opened_before is None
            or not stat_module.S_ISREG(opened_before.st_mode)
            or opened_before.st_uid != os.geteuid()
            or opened_before.st_nlink != 1
            or stat_module.S_IMODE(opened_before.st_mode) != expected_mode
            or opened_before.st_size > max_bytes
            or _identity(opened_before) != _identity(entry_before)
        ):
            raise StateError("run ledger changed during verification")
        content = bytearray()
        while True:
            try:
                chunk = os.read(file_fd, min(64 * 1024, max_bytes + 1 - len(content)))
            except OSError:
                raise StateError("run ledger changed during verification") from None
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise StateError("run ledger changed during verification")
        opened_after = _safe_fstat(file_fd)
        entry_after = _safe_stat(filename, directory_fd=directory_fd)
        before_metadata = (
            opened_before.st_dev,
            opened_before.st_ino,
            opened_before.st_mode,
            opened_before.st_uid,
            opened_before.st_nlink,
            opened_before.st_size,
            opened_before.st_mtime_ns,
            opened_before.st_ctime_ns,
        )
        if (
            opened_after is None
            or entry_after is None
            or before_metadata
            != (
                entry_before.st_dev,
                entry_before.st_ino,
                entry_before.st_mode,
                entry_before.st_uid,
                entry_before.st_nlink,
                entry_before.st_size,
                entry_before.st_mtime_ns,
                entry_before.st_ctime_ns,
            )
            or before_metadata
            != (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_mode,
                opened_after.st_uid,
                opened_after.st_nlink,
                opened_after.st_size,
                opened_after.st_mtime_ns,
                opened_after.st_ctime_ns,
            )
            or before_metadata
            != (
                entry_after.st_dev,
                entry_after.st_ino,
                entry_after.st_mode,
                entry_after.st_uid,
                entry_after.st_nlink,
                entry_after.st_size,
                entry_after.st_mtime_ns,
                entry_after.st_ctime_ns,
            )
        ):
            raise StateError("run ledger changed during verification")
        return bytes(content)
    finally:
        _close_noerror(file_fd)


def _safe_relative_components(relative_name: str) -> tuple[str, ...]:
    if not isinstance(relative_name, str) or not relative_name or "\x00" in relative_name:
        raise StateError("invalid reserved run file name")
    components = tuple(relative_name.split("/"))
    if any(not component or component in {".", ".."} or "/" in component for component in components):
        raise StateError("invalid reserved run file name")
    return components


def _verify_pinned_new_run_authority(reservation: NewRunReservation) -> None:
    """Verify open reservation objects without trusting their visible paths."""
    runs_open = _safe_fstat(reservation._runs_fd)
    run_open = _safe_fstat(reservation._run_fd)
    locks_open = _safe_fstat(reservation._locks_fd)
    lock_open = _safe_fstat(reservation._lock_fd)
    if (
        runs_open is None
        or run_open is None
        or locks_open is None
        or lock_open is None
        or not stat_module.S_ISDIR(runs_open.st_mode)
        or not stat_module.S_ISDIR(run_open.st_mode)
        or not stat_module.S_ISDIR(locks_open.st_mode)
        or not stat_module.S_ISREG(lock_open.st_mode)
        or lock_open.st_uid != os.geteuid()
        or lock_open.st_nlink != 1
        or stat_module.S_IMODE(lock_open.st_mode) != 0o600
        or _identity(runs_open) != reservation._runs_identity
        or _identity(run_open) != reservation._run_identity
        or _identity(locks_open) != reservation._locks_identity
        or _identity(lock_open) != reservation._lock_identity
        or _read_exact_private_file(reservation._run_fd, "owner.json") != _json_bytes(asdict(reservation.record))
    ):
        raise StateError("invalid pinned new run reservation authority")


def _open_reserved_parent(reservation: NewRunReservation, components: tuple[str, ...]) -> tuple[int, list[int]]:
    parent_fd = reservation._run_fd
    opened: list[int] = []
    for component in components[:-1]:
        next_fd = _open_readonly_no_follow(component, directory_fd=parent_fd, directory=True)
        if next_fd is None:
            for file_fd in reversed(opened):
                _close_noerror(file_fd)
            raise StateError("cannot securely open reserved run file parent")
        metadata = _safe_fstat(next_fd)
        if metadata is None or not stat_module.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _close_noerror(next_fd)
            for file_fd in reversed(opened):
                _close_noerror(file_fd)
            raise StateError("cannot securely open reserved run file parent")
        opened.append(next_fd)
        parent_fd = next_fd
    return parent_fd, opened


def _write_reserved_run_file(
    reservation: NewRunReservation,
    relative_name: str,
    content: bytes,
    *,
    mode: int,
) -> None:
    if not isinstance(content, bytes) or mode != 0o600:
        raise StateError("invalid reserved run file payload")
    components = _safe_relative_components(relative_name)
    reservation.verify_binding()
    parent_fd, opened = _open_reserved_parent(reservation, components)
    temporary = f".artifact-tmp-{uuid.uuid4().hex}"
    temporary_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        temporary_fd = os.open(temporary, flags, mode, dir_fd=parent_fd)
        os.fchmod(temporary_fd, mode)
        _write_all(temporary_fd, content)
        os.fsync(temporary_fd)
        temporary_stat = _safe_fstat(temporary_fd)
        if (
            temporary_stat is None
            or not stat_module.S_ISREG(temporary_stat.st_mode)
            or temporary_stat.st_uid != os.geteuid()
            or temporary_stat.st_nlink != 1
            or stat_module.S_IMODE(temporary_stat.st_mode) != mode
        ):
            raise StateError("cannot durably write reserved run file")
        reservation.verify_binding()
        os.replace(temporary, components[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = ""
        os.fsync(parent_fd)
        published = _safe_stat(components[-1], directory_fd=parent_fd)
        if (
            published is None
            or not stat_module.S_ISREG(published.st_mode)
            or _identity(published) != _identity(temporary_stat)
            or _read_exact_private_file(parent_fd, components[-1], max_bytes=len(content)) != content
        ):
            raise StateError("reserved run file changed during write")
        reservation.verify_binding()
    except OSError:
        raise StateError("cannot durably write reserved run file") from None
    finally:
        if temporary_fd is not None:
            _close_noerror(temporary_fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        for file_fd in reversed(opened):
            _close_noerror(file_fd)


def _sync_staged_tree(directory_fd: int) -> None:
    try:
        names = os.listdir(directory_fd)
    except OSError:
        raise StateError("cannot securely inspect staged run artifacts") from None
    for name in names:
        if not isinstance(name, str) or name in {".", ".."} or "/" in name or "\x00" in name:
            raise StateError("invalid staged run artifact")
        metadata = _safe_stat(name, directory_fd=directory_fd)
        if metadata is None or metadata.st_uid != os.geteuid():
            raise StateError("invalid staged run artifact")
        if stat_module.S_ISDIR(metadata.st_mode):
            child_fd = _open_readonly_no_follow(name, directory_fd=directory_fd, directory=True)
            if child_fd is None:
                raise StateError("invalid staged run artifact")
            try:
                os.fchmod(child_fd, 0o700)
                _sync_staged_tree(child_fd)
                os.fsync(child_fd)
            except OSError:
                raise StateError("cannot durably stage run artifacts") from None
            finally:
                _close_noerror(child_fd)
            continue
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise StateError("invalid staged run artifact")
        file_fd = _open_readonly_no_follow(name, directory_fd=directory_fd, nonblocking=True)
        if file_fd is None:
            raise StateError("invalid staged run artifact")
        try:
            os.fchmod(file_fd, 0o600)
            os.fsync(file_fd)
        except OSError:
            raise StateError("cannot durably stage run artifacts") from None
        finally:
            _close_noerror(file_fd)


def _publish_reserved_run_staging(reservation: NewRunReservation, staging_root: Path) -> None:
    reservation.verify_binding()
    staging_fd = _open_readonly_no_follow(staging_root, directory=True)
    if staging_fd is None:
        raise StateError("cannot securely open staged run artifacts")
    try:
        staging_stat = _safe_fstat(staging_fd)
        if staging_stat is None or not stat_module.S_ISDIR(staging_stat.st_mode) or staging_stat.st_uid != os.geteuid():
            raise StateError("cannot securely open staged run artifacts")
        _sync_staged_tree(staging_fd)
        names = sorted(os.listdir(staging_fd))
        if not names or {"owner.json", "state.json"}.intersection(names):
            raise StateError("invalid staged run artifact set")
        for name in names:
            if _safe_stat(name, directory_fd=reservation._run_fd) is not None:
                raise StateError("reserved run artifact already exists")
            reservation.verify_binding()
            try:
                os.rename(name, name, src_dir_fd=staging_fd, dst_dir_fd=reservation._run_fd)
            except OSError:
                raise StateError("cannot publish staged run artifacts") from None
            os.fsync(reservation._run_fd)
        os.fsync(staging_fd)
        reservation.verify_binding()
    except OSError:
        raise StateError("cannot durably publish staged run artifacts") from None
    finally:
        _close_noerror(staging_fd)


def _verify_new_run_binding(reservation: NewRunReservation) -> None:
    if (
        not isinstance(reservation.roots, StatePaths)
        or not isinstance(reservation.paths, RunPaths)
        or not isinstance(reservation.record, OwnerRecord)
        or reservation.record.schema_version != 1
        or reservation.record.name != reservation.paths.name
        or not isinstance(reservation.dispatch_key, DispatchKey)
    ):
        raise StateError("invalid new run reservation authority")
    parsed_run_id: uuid.UUID | None = None
    try:
        parsed_run_id = uuid.UUID(reservation.record.run_id)
    except (AttributeError, TypeError, ValueError):
        pass
    if parsed_run_id is None or str(parsed_run_id) != reservation.record.run_id:
        raise StateError("invalid new run reservation authority")
    runs_open = _safe_fstat(reservation._runs_fd)
    runs_path = _safe_stat(reservation.roots.runs)
    public_runs_parent = _safe_stat(reservation.paths.root.parent)
    run_open = _safe_fstat(reservation._run_fd)
    run_entry = _safe_stat(reservation.paths.name, directory_fd=reservation._runs_fd)
    locks_open = _safe_fstat(reservation._locks_fd)
    locks_path = _safe_stat(reservation.roots.locks)
    public_locks_parent = _safe_stat(reservation.paths.lock.parent)
    lock_open = _safe_fstat(reservation._lock_fd)
    lock_entry = _safe_stat(f"{reservation.paths.name}.lock", directory_fd=reservation._locks_fd)
    if (
        runs_open is None
        or runs_path is None
        or public_runs_parent is None
        or run_open is None
        or run_entry is None
        or not stat_module.S_ISDIR(runs_open.st_mode)
        or not stat_module.S_ISDIR(runs_path.st_mode)
        or not stat_module.S_ISDIR(run_open.st_mode)
        or not stat_module.S_ISDIR(run_entry.st_mode)
        or _identity(runs_open) != reservation._runs_identity
        or _identity(runs_path) != reservation._runs_identity
        or _identity(public_runs_parent) != reservation._runs_identity
        or _identity(run_open) != reservation._run_identity
        or _identity(run_entry) != reservation._run_identity
        or locks_open is None
        or locks_path is None
        or public_locks_parent is None
        or lock_open is None
        or lock_entry is None
        or not stat_module.S_ISDIR(locks_open.st_mode)
        or not stat_module.S_ISDIR(locks_path.st_mode)
        or not stat_module.S_ISREG(lock_open.st_mode)
        or not stat_module.S_ISREG(lock_entry.st_mode)
        or lock_open.st_uid != os.geteuid()
        or lock_open.st_nlink != 1
        or stat_module.S_IMODE(lock_open.st_mode) != 0o600
        or _identity(locks_open) != reservation._locks_identity
        or _identity(locks_path) != reservation._locks_identity
        or _identity(public_locks_parent) != reservation._locks_identity
        or _identity(lock_open) != reservation._lock_identity
        or _identity(lock_entry) != reservation._lock_identity
    ):
        raise StateError("new run reservation changed during create")


def _write_exclusive_owner(run_fd: int, record: OwnerRecord) -> None:
    payload = asdict(record)
    content = _json_bytes(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        owner_fd = os.open("owner.json", flags, 0o600, dir_fd=run_fd)
    except OSError:
        raise StateError("cannot exclusively create owner record") from None
    try:
        os.fchmod(owner_fd, 0o600)
        _write_all(owner_fd, content)
        os.fsync(owner_fd)
        owner_open = _safe_fstat(owner_fd)
        if (
            owner_open is None
            or not stat_module.S_ISREG(owner_open.st_mode)
            or owner_open.st_uid != os.geteuid()
            or owner_open.st_nlink != 1
            or stat_module.S_IMODE(owner_open.st_mode) != 0o600
        ):
            raise StateError("cannot exclusively create owner record")
    except OSError:
        raise StateError("cannot durably create owner record") from None
    finally:
        _close_noerror(owner_fd)
    owner_entry = _safe_stat("owner.json", directory_fd=run_fd)
    if (
        owner_entry is None
        or not stat_module.S_ISREG(owner_entry.st_mode)
        or owner_entry.st_uid != os.geteuid()
        or owner_entry.st_nlink != 1
        or stat_module.S_IMODE(owner_entry.st_mode) != 0o600
        or _identity(owner_entry) != _identity(owner_open)
    ):
        raise StateError("owner record changed during create")
    try:
        os.fsync(run_fd)
    except OSError:
        raise StateError("cannot durably create owner record") from None
    if _read_exact_private_file(run_fd, "owner.json") != content:
        raise StateError("owner record changed during create")


_RESERVED_RUN_STATE_FIELDS = frozenset(
    {"schema_version", "runtime_kind", "backend", "name", "run_id", "status", "lifecycle_revision"}
)


def _require_run_artifact_target(state_root: Path, digest: Any, local_path: Any = None) -> None:
    if not isinstance(digest, str):
        raise StateError("run ledger artifact digest is invalid")
    normalized = normalize_digest(digest)
    target = (
        Path(local_path)
        if isinstance(local_path, str)
        else state_root / "store" / "blobs" / "sha256" / normalized.split(":", 1)[1]
    )
    try:
        entry = target.stat(follow_symlinks=False)
    except OSError:
        raise StateError("run ledger references a missing artifact") from None
    if not stat_module.S_ISREG(entry.st_mode):
        raise StateError("run ledger references an unsafe artifact")


def _validate_run_reference_targets(state_root: Path, payload: Mapping[str, Any]) -> None:
    if "base" in payload:
        base = payload["base"]
        if not isinstance(base, Mapping):
            raise StateError("run ledger base reference is invalid")
        if "digest" in base:
            _require_run_artifact_target(state_root, base["digest"], base.get("local_path"))
    if "base_digest" in payload and payload["base_digest"] is not None:
        _require_run_artifact_target(state_root, payload["base_digest"])
    if "layers" in payload:
        layers = payload["layers"]
        if not isinstance(layers, (list, tuple)):
            raise StateError("run ledger layers must be a sequence")
        for layer in layers:
            if isinstance(layer, str):
                _require_run_artifact_target(state_root, layer)
            elif isinstance(layer, Mapping) and "digest" in layer:
                _require_run_artifact_target(state_root, layer["digest"], layer.get("local_path"))
            else:
                raise StateError("run ledger layer reference is invalid")


def _write_reserved_run_state(
    reservation: NewRunReservation,
    status: str,
    data: Mapping[str, Any],
    *,
    require_visible_binding: bool = True,
) -> dict[str, Any]:
    if not isinstance(status, str) or status not in ALLOWED_RUNTIME_STATUSES[reservation.dispatch_key.runtime_kind]:
        raise StateError("invalid status for new run ledger")
    if not isinstance(data, Mapping):
        raise StateError("new run state data must be a mapping")
    if _RESERVED_RUN_STATE_FIELDS.intersection(data):
        raise StateError("new run state cannot override reserved identity fields")
    payload = {
        **dict(data),
        "schema_version": 2,
        "runtime_kind": reservation.dispatch_key.runtime_kind.value,
        "backend": reservation.dispatch_key.backend.value,
        "name": reservation.record.name,
        "run_id": reservation.record.run_id,
        "status": status,
        "lifecycle_revision": reservation.lifecycle_revision + 1,
    }
    _validate_run_reference_targets(reservation.roots.state, payload)
    content = _json_bytes(payload)

    def verify() -> None:
        if require_visible_binding:
            reservation.verify_binding()
        else:
            _verify_pinned_new_run_authority(reservation)

    verify()
    temporary = f".state-tmp-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    temporary_fd: int | None = None
    try:
        temporary_fd = os.open(temporary, flags, 0o600, dir_fd=reservation._run_fd)
        os.fchmod(temporary_fd, 0o600)
        _write_all(temporary_fd, content)
        os.fsync(temporary_fd)
        temporary_open = _safe_fstat(temporary_fd)
        if (
            temporary_open is None
            or not stat_module.S_ISREG(temporary_open.st_mode)
            or temporary_open.st_uid != os.geteuid()
            or temporary_open.st_nlink != 1
            or stat_module.S_IMODE(temporary_open.st_mode) != 0o600
        ):
            raise StateError("cannot durably write new run ledger")
        verify()
        os.replace(
            temporary,
            "state.json",
            src_dir_fd=reservation._run_fd,
            dst_dir_fd=reservation._run_fd,
        )
        temporary = ""
        os.fsync(reservation._run_fd)
        state_entry = _safe_stat("state.json", directory_fd=reservation._run_fd)
        if (
            state_entry is None
            or not stat_module.S_ISREG(state_entry.st_mode)
            or state_entry.st_uid != os.geteuid()
            or state_entry.st_nlink != 1
            or stat_module.S_IMODE(state_entry.st_mode) != 0o600
            or _identity(state_entry) != _identity(temporary_open)
        ):
            raise StateError("new run state changed during write")
        verify()
        if _read_exact_private_file(reservation._run_fd, "state.json") != content:
            raise StateError("new run state changed during write")
        return payload
    except OSError:
        raise StateError("cannot durably write new run ledger") from None
    finally:
        if temporary_fd is not None:
            _close_noerror(temporary_fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=reservation._run_fd)
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class _NewRunNameLock:
    locks_fd: int
    lock_fd: int
    locks_identity: tuple[int, int]
    lock_identity: tuple[int, int]


@contextmanager
def _new_run_name_lock(roots: StatePaths, name: str, *, lock_timeout: float | None = None) -> Iterator[_NewRunNameLock]:
    if lock_timeout is not None and (
        type(lock_timeout) not in {int, float} or not math.isfinite(lock_timeout) or lock_timeout <= 0
    ):
        raise StateError("invalid run lock timeout")
    deadline = None if lock_timeout is None else time.monotonic() + lock_timeout
    locks_fd = _open_readonly_no_follow(roots.locks, directory=True)
    if locks_fd is None:
        raise StateError("cannot securely lock new run name")
    lock_fd: int | None = None
    filename = f"{name}.lock"
    try:
        locks_open = _safe_fstat(locks_fd)
        locks_path = _safe_stat(roots.locks)
        if (
            locks_open is None
            or locks_path is None
            or not stat_module.S_ISDIR(locks_open.st_mode)
            or not stat_module.S_ISDIR(locks_path.st_mode)
            or _identity(locks_open) != _identity(locks_path)
        ):
            raise StateError("cannot securely lock new run name")
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        prior_entry = _safe_stat(filename, directory_fd=locks_fd)
        try:
            lock_fd = os.open(filename, flags, 0o600, dir_fd=locks_fd)
        except OSError:
            raise StateError("cannot securely lock new run name") from None
        lock_open = _safe_fstat(lock_fd)
        if (
            lock_open is None
            or not stat_module.S_ISREG(lock_open.st_mode)
            or lock_open.st_uid != os.geteuid()
            or lock_open.st_nlink != 1
        ):
            raise StateError("cannot securely lock new run name")
        try:
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
            if prior_entry is None:
                os.fsync(locks_fd)
        except OSError:
            raise StateError("cannot securely lock new run name") from None
        lock_open = _safe_fstat(lock_fd)
        lock_entry = _safe_stat(filename, directory_fd=locks_fd)
        if (
            lock_open is None
            or lock_entry is None
            or not stat_module.S_ISREG(lock_open.st_mode)
            or not stat_module.S_ISREG(lock_entry.st_mode)
            or lock_open.st_uid != os.geteuid()
            or lock_open.st_nlink != 1
            or stat_module.S_IMODE(lock_open.st_mode) != 0o600
            or _identity(lock_open) != _identity(lock_entry)
        ):
            raise StateError("cannot securely lock new run name")
        try:
            if deadline is None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            else:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StateError("run lock timed out")
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(min(0.01, remaining))
        except OSError:
            raise StateError("cannot securely lock new run name") from None
        current = _safe_stat(filename, directory_fd=locks_fd)
        current_locks = _safe_stat(roots.locks)
        if (
            current is None
            or current_locks is None
            or _identity(current) != _identity(lock_open)
            or _identity(current_locks) != _identity(locks_open)
        ):
            raise StateError("new run name lock changed during acquire")
        yield _NewRunNameLock(
            locks_fd=locks_fd,
            lock_fd=lock_fd,
            locks_identity=_identity(locks_open),
            lock_identity=_identity(lock_open),
        )
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            _close_noerror(lock_fd)
        _close_noerror(locks_fd)


@contextmanager
def reserve_new_run(
    roots: StatePaths,
    name: str,
    dispatch_key: DispatchKey,
) -> Iterator[NewRunReservation]:
    """Exclusively reserve and pin one new run directory for its full create."""
    if not isinstance(dispatch_key, DispatchKey):
        raise StateError("new run reservation requires a DispatchKey")
    rpaths = _new_run_paths(roots, name)
    with _new_run_name_lock(roots, name) as name_lock:
        runs_fd = _open_readonly_no_follow(roots.runs, directory=True)
        if runs_fd is None:
            raise StateError("cannot securely reserve new run")
        run_fd: int | None = None
        created_entry = False
        owner_created = False
        try:
            runs_open = _safe_fstat(runs_fd)
            runs_path = _safe_stat(roots.runs)
            if (
                runs_open is None
                or runs_path is None
                or not stat_module.S_ISDIR(runs_open.st_mode)
                or not stat_module.S_ISDIR(runs_path.st_mode)
                or _identity(runs_open) != _identity(runs_path)
            ):
                raise StateError("cannot securely reserve new run")
            try:
                os.mkdir(name, 0o700, dir_fd=runs_fd)
                created_entry = True
            except FileExistsError:
                removed = False
                try:
                    _raw_owner, raw_state = _read_pinned_run_payloads(runs_fd, name)
                    removed = raw_state.get("status") == "removed"
                except (RecursionError, StateError, TypeError, ValueError):
                    pass
                if removed:
                    raise StateError(
                        f"run name '{name}' is held by a removed run; free it with: palimpsest rm {name} --volumes"
                    ) from None
                raise StateError(f"run name '{name}' already exists") from None
            except OSError:
                raise StateError("cannot securely reserve new run") from None
            try:
                os.fsync(runs_fd)
            except OSError:
                raise StateError("cannot durably reserve new run") from None
            entry = _safe_stat(name, directory_fd=runs_fd)
            run_fd = _open_readonly_no_follow(name, directory_fd=runs_fd, directory=True)
            run_open = None if run_fd is None else _safe_fstat(run_fd)
            if (
                entry is None
                or run_fd is None
                or run_open is None
                or not stat_module.S_ISDIR(entry.st_mode)
                or not stat_module.S_ISDIR(run_open.st_mode)
                or _identity(entry) != _identity(run_open)
            ):
                raise StateError("cannot securely reserve new run")
            record = OwnerRecord(1, str(uuid.uuid4()), name)
            _write_exclusive_owner(run_fd, record)
            owner_created = True
            reservation = NewRunReservation(
                roots,
                rpaths,
                record,
                dispatch_key,
                runs_fd,
                run_fd,
                name_lock.locks_fd,
                name_lock.lock_fd,
                _identity(runs_open),
                _identity(run_open),
                name_lock.locks_identity,
                name_lock.lock_identity,
            )
            reservation.verify_binding()
            try:
                yield reservation
            except BaseException:
                if reservation.last_status != "failed":
                    try:
                        reservation.write_failure({"error": "run creation failed"})
                    except BaseException:
                        pass
                raise
        finally:
            if created_entry and not owner_created:
                current_entry = _safe_stat(name, directory_fd=runs_fd)
                pinned_entry = None if run_fd is None else _safe_fstat(run_fd)
                if (
                    current_entry is not None
                    and pinned_entry is not None
                    and _identity(current_entry) == _identity(pinned_entry)
                ):
                    try:
                        os.unlink("owner.json", dir_fd=run_fd)
                    except OSError:
                        pass
                    try:
                        os.rmdir(name, dir_fd=runs_fd)
                        os.fsync(runs_fd)
                    except OSError:
                        pass
            if run_fd is not None:
                _close_noerror(run_fd)
            _close_noerror(runs_fd)


class ExistingRunMutation:
    """Pinned authority for one existing-run lifecycle mutation."""

    __slots__ = (
        "_roots",
        "_paths",
        "_record",
        "_snapshot",
        "_initial_snapshot",
        "_owner_bytes",
        "_state_bytes",
        "_runs_fd",
        "_run_fd",
        "_locks_fd",
        "_lock_fd",
        "_runs_identity",
        "_run_identity",
        "_locks_identity",
        "_lock_identity",
        "_deleted",
    )

    def __init__(
        self,
        roots: StatePaths,
        paths: RunPaths,
        snapshot: RunLedgerSnapshot,
        owner_bytes: bytes,
        state_bytes: bytes,
        runs_fd: int,
        run_fd: int,
        name_lock: _NewRunNameLock,
        runs_identity: tuple[int, int],
        run_identity: tuple[int, int],
    ) -> None:
        object.__setattr__(self, "_roots", roots)
        object.__setattr__(self, "_paths", paths)
        object.__setattr__(self, "_record", snapshot.record)
        object.__setattr__(self, "_snapshot", snapshot)
        object.__setattr__(self, "_initial_snapshot", snapshot)
        object.__setattr__(self, "_owner_bytes", owner_bytes)
        object.__setattr__(self, "_state_bytes", state_bytes)
        object.__setattr__(self, "_runs_fd", runs_fd)
        object.__setattr__(self, "_run_fd", run_fd)
        object.__setattr__(self, "_locks_fd", name_lock.locks_fd)
        object.__setattr__(self, "_lock_fd", name_lock.lock_fd)
        object.__setattr__(self, "_runs_identity", runs_identity)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_locks_identity", name_lock.locks_identity)
        object.__setattr__(self, "_lock_identity", name_lock.lock_identity)
        object.__setattr__(self, "_deleted", False)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("existing run mutation authority is immutable")

    @property
    def paths(self) -> RunPaths:
        return self._paths

    @property
    def record(self) -> ExistingRunRecord:
        return self._record

    @property
    def snapshot(self) -> RunLedgerSnapshot:
        return self._snapshot

    @property
    def initial_snapshot(self) -> RunLedgerSnapshot:
        return self._initial_snapshot

    @property
    def is_legacy(self) -> bool:
        return self._record.state_schema_version == 1

    def mutable_state(self) -> dict[str, Any]:
        def thaw(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {key: thaw(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [thaw(item) for item in value]
            return value

        return thaw(self._snapshot.state)

    def verify_binding(self) -> None:
        _verify_existing_run_mutation(self)

    def read_ssh_trust_artifacts(self) -> tuple[bytes, bytes]:
        """Read exact owner-only SSH bytes beneath this pinned run."""

        self.verify_binding()
        ssh_entry = _safe_stat("ssh", directory_fd=self._run_fd)
        ssh_fd = _open_readonly_no_follow("ssh", directory_fd=self._run_fd, directory=True)
        try:
            ssh_open = None if ssh_fd is None else _safe_fstat(ssh_fd)
            if (
                ssh_entry is None
                or ssh_fd is None
                or ssh_open is None
                or not stat_module.S_ISDIR(ssh_entry.st_mode)
                or not stat_module.S_ISDIR(ssh_open.st_mode)
                or ssh_entry.st_uid != os.geteuid()
                or ssh_open.st_uid != os.geteuid()
                or stat_module.S_IMODE(ssh_entry.st_mode) != 0o700
                or stat_module.S_IMODE(ssh_open.st_mode) != 0o700
                or _identity(ssh_entry) != _identity(ssh_open)
            ):
                raise StateError("run SSH trust artifacts changed during verification")
            identity = _read_exact_private_file(ssh_fd, "id_ed25519")
            known_hosts = _read_exact_private_file(ssh_fd, "known_hosts")
            current = _safe_stat("ssh", directory_fd=self._run_fd)
            if current is None or _identity(current) != _identity(ssh_open):
                raise StateError("run SSH trust artifacts changed during verification")
        finally:
            if ssh_fd is not None:
                _close_noerror(ssh_fd)
        self.verify_binding()
        return identity, known_hosts

    def write_state(self, status: str, data: Mapping[str, Any]) -> dict[str, Any]:
        with artifact_reference_guard(self._roots):
            return _write_existing_run_mutation_state(self, status, data)

    def write_file(self, relative_name: str, content: bytes, *, mode: int = 0o600) -> None:
        _write_existing_run_file(self, relative_name, content, append=False, mode=mode)

    def append_file(self, relative_name: str, content: bytes) -> None:
        _write_existing_run_file(self, relative_name, content, append=True, mode=0o600)

    def delete_run_tree(self) -> None:
        _delete_existing_run_tree(self)


def _decode_exact_json_object(content: bytes) -> dict[str, Any]:
    decoded: str | None = None
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    parsed = False
    value: Any = None
    if decoded is not None:
        try:
            value = json.loads(decoded)
            parsed = True
        except (RecursionError, ValueError):
            pass
    if not parsed or not isinstance(value, dict):
        raise StateError("cannot securely read existing run ledger")
    return value


def _verify_existing_run_mutation(
    mutation: ExistingRunMutation,
    *,
    expected_state_bytes: bytes | None = None,
) -> None:
    if mutation._deleted:
        raise StateError("existing run was already deleted")
    state_bytes = mutation._state_bytes if expected_state_bytes is None else expected_state_bytes
    runs_open = _safe_fstat(mutation._runs_fd)
    runs_path = _safe_stat(mutation._roots.runs)
    public_runs_parent = _safe_stat(mutation._paths.root.parent)
    run_open = _safe_fstat(mutation._run_fd)
    run_entry = _safe_stat(mutation._paths.name, directory_fd=mutation._runs_fd)
    locks_open = _safe_fstat(mutation._locks_fd)
    locks_path = _safe_stat(mutation._roots.locks)
    public_locks_parent = _safe_stat(mutation._paths.lock.parent)
    lock_open = _safe_fstat(mutation._lock_fd)
    lock_entry = _safe_stat(f"{mutation._paths.name}.lock", directory_fd=mutation._locks_fd)
    if (
        runs_open is None
        or runs_path is None
        or public_runs_parent is None
        or run_open is None
        or run_entry is None
        or locks_open is None
        or locks_path is None
        or public_locks_parent is None
        or lock_open is None
        or lock_entry is None
        or not stat_module.S_ISDIR(runs_open.st_mode)
        or not stat_module.S_ISDIR(runs_path.st_mode)
        or not stat_module.S_ISDIR(run_open.st_mode)
        or not stat_module.S_ISDIR(run_entry.st_mode)
        or not stat_module.S_ISDIR(locks_open.st_mode)
        or not stat_module.S_ISDIR(locks_path.st_mode)
        or not stat_module.S_ISREG(lock_open.st_mode)
        or not stat_module.S_ISREG(lock_entry.st_mode)
        or lock_open.st_uid != os.geteuid()
        or lock_open.st_nlink != 1
        or stat_module.S_IMODE(lock_open.st_mode) != 0o600
        or _identity(runs_open) != mutation._runs_identity
        or _identity(runs_path) != mutation._runs_identity
        or _identity(public_runs_parent) != mutation._runs_identity
        or _identity(run_open) != mutation._run_identity
        or _identity(run_entry) != mutation._run_identity
        or _identity(locks_open) != mutation._locks_identity
        or _identity(locks_path) != mutation._locks_identity
        or _identity(public_locks_parent) != mutation._locks_identity
        or _identity(lock_open) != mutation._lock_identity
        or _identity(lock_entry) != mutation._lock_identity
        or _read_exact_private_file(mutation._run_fd, "owner.json") != mutation._owner_bytes
        or _read_exact_private_file(mutation._run_fd, "state.json") != state_bytes
    ):
        raise StateError("existing run changed during lifecycle mutation")


def _validated_existing_state_payload(
    mutation: ExistingRunMutation,
    status: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(status, str) or status not in ALLOWED_RUNTIME_STATUSES[mutation.record.dispatch_key.runtime_kind]:
        raise StateError("invalid status for existing run ledger")
    if not isinstance(data, Mapping):
        raise StateError("existing run state data must be a mapping")
    current = mutation.snapshot.state
    identity_values = {
        "runtime_kind": mutation.record.dispatch_key.runtime_kind.value,
        "backend": mutation.record.dispatch_key.backend.value,
        "name": mutation.record.name,
        "run_id": mutation.record.run_id,
    }
    for key, expected in identity_values.items():
        if key in data and data[key] != expected:
            raise StateError("existing run state cannot override durable identity")
    if "schema_version" in data and data["schema_version"] != mutation.record.state_schema_version:
        raise StateError("existing run state cannot override durable schema")
    if "status" in data and data["status"] != current.get("status"):
        raise StateError("existing run state is stale")
    current_revision = lifecycle_revision(current)
    if current_revision >= _MAX_LIFECYCLE_REVISION:
        raise StateError("lifecycle revision cannot be incremented")
    body = {key: deepcopy(value) for key, value in data.items() if key not in _RESERVED_RUN_STATE_FIELDS}
    return {
        **body,
        "schema_version": 2,
        "runtime_kind": mutation.record.dispatch_key.runtime_kind.value,
        "backend": mutation.record.dispatch_key.backend.value,
        "name": mutation.record.name,
        "run_id": mutation.record.run_id,
        "status": status,
        "lifecycle_revision": current_revision + 1,
    }


def _write_existing_run_file(
    mutation: ExistingRunMutation,
    relative_name: str,
    content: bytes,
    *,
    append: bool,
    mode: int,
) -> None:
    if (
        not isinstance(content, bytes)
        or type(mode) is not int
        or mode not in {0o400, 0o600}
        or (append and mode != 0o600)
    ):
        raise StateError("invalid existing run file payload")
    components = _safe_relative_components(relative_name)
    mutation.verify_binding()
    parent_fd = mutation._run_fd
    opened: list[int] = []
    file_fd: int | None = None
    temporary: str | None = None
    try:
        for component in components[:-1]:
            child_fd = _open_readonly_no_follow(component, directory_fd=parent_fd, directory=True)
            if child_fd is None:
                raise StateError("cannot securely open existing run file parent")
            child = _safe_fstat(child_fd)
            if child is None or not stat_module.S_ISDIR(child.st_mode) or child.st_uid != os.geteuid():
                _close_noerror(child_fd)
                raise StateError("cannot securely open existing run file parent")
            opened.append(child_fd)
            parent_fd = child_fd
        if append:
            prior = _safe_stat(components[-1], directory_fd=parent_fd)
            created = prior is None
            if prior is not None and (
                not stat_module.S_ISREG(prior.st_mode)
                or prior.st_uid != os.geteuid()
                or prior.st_nlink != 1
                or stat_module.S_IMODE(prior.st_mode) != 0o600
            ):
                raise StateError("cannot securely append existing run file")
            flags = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if created:
                flags |= os.O_CREAT | os.O_EXCL
            file_fd = os.open(components[-1], flags, 0o600, dir_fd=parent_fd)
            if created:
                os.fchmod(file_fd, 0o600)
            before = _safe_fstat(file_fd)
            if (
                before is None
                or not stat_module.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat_module.S_IMODE(before.st_mode) != 0o600
                or (prior is not None and _identity(before) != _identity(prior))
            ):
                raise StateError("cannot securely append existing run file")
            _write_all(file_fd, content)
            os.fsync(file_fd)
            if created:
                os.fsync(parent_fd)
            current = _safe_stat(components[-1], directory_fd=parent_fd)
            after = _safe_fstat(file_fd)
            if (
                current is None
                or after is None
                or _identity(current) != _identity(before)
                or _identity(after) != _identity(before)
            ):
                raise StateError("existing run file changed during append")
        else:
            temporary = f".artifact-mutation-{uuid.uuid4().hex}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            file_fd = os.open(temporary, flags, mode, dir_fd=parent_fd)
            os.fchmod(file_fd, mode)
            _write_all(file_fd, content)
            os.fsync(file_fd)
            staged = _safe_fstat(file_fd)
            if (
                staged is None
                or not stat_module.S_ISREG(staged.st_mode)
                or staged.st_uid != os.geteuid()
                or staged.st_nlink != 1
                or stat_module.S_IMODE(staged.st_mode) != mode
            ):
                raise StateError("cannot securely write existing run file")
            mutation.verify_binding()
            os.replace(temporary, components[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temporary = None
            os.fsync(parent_fd)
            published = _safe_stat(components[-1], directory_fd=parent_fd)
            if (
                published is None
                or _identity(published) != _identity(staged)
                or _read_exact_private_file(
                    parent_fd,
                    components[-1],
                    max_bytes=max(len(content), 1),
                    expected_mode=mode,
                )
                != content
            ):
                raise StateError("existing run file changed during write")
        mutation.verify_binding()
    except OSError:
        raise StateError("cannot durably write existing run file") from None
    finally:
        if file_fd is not None:
            _close_noerror(file_fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        for opened_fd in reversed(opened):
            _close_noerror(opened_fd)


def _write_existing_run_mutation_state(
    mutation: ExistingRunMutation,
    status: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _validated_existing_state_payload(mutation, status, data)
    _validate_run_reference_targets(mutation._roots.state, payload)
    content = _json_bytes(payload)
    mutation.verify_binding()
    temporary = f".state-mutation-{uuid.uuid4().hex}"
    temporary_fd: int | None = None
    published = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        temporary_fd = os.open(temporary, flags, 0o600, dir_fd=mutation._run_fd)
        os.fchmod(temporary_fd, 0o600)
        _write_all(temporary_fd, content)
        os.fsync(temporary_fd)
        temporary_stat = _safe_fstat(temporary_fd)
        if (
            temporary_stat is None
            or not stat_module.S_ISREG(temporary_stat.st_mode)
            or temporary_stat.st_uid != os.geteuid()
            or temporary_stat.st_nlink != 1
            or stat_module.S_IMODE(temporary_stat.st_mode) != 0o600
        ):
            raise StateError("cannot durably write existing run ledger")
        mutation.verify_binding()
        os.replace(
            temporary,
            "state.json",
            src_dir_fd=mutation._run_fd,
            dst_dir_fd=mutation._run_fd,
        )
        temporary = ""
        published = True
        os.fsync(mutation._run_fd)
        published_stat = _safe_stat("state.json", directory_fd=mutation._run_fd)
        if (
            published_stat is None
            or not stat_module.S_ISREG(published_stat.st_mode)
            or published_stat.st_uid != os.geteuid()
            or published_stat.st_nlink != 1
            or stat_module.S_IMODE(published_stat.st_mode) != 0o600
            or _identity(published_stat) != _identity(temporary_stat)
            or _read_exact_private_file(mutation._run_fd, "state.json") != content
        ):
            raise StateError("existing run state changed during write")
        _verify_existing_run_mutation(mutation, expected_state_bytes=content)
        raw_owner = _decode_exact_json_object(mutation._owner_bytes)
        snapshot = _snapshot_from_payloads(mutation.record.name, raw_owner, payload)
        object.__setattr__(mutation, "_state_bytes", content)
        object.__setattr__(mutation, "_snapshot", snapshot)
        object.__setattr__(mutation, "_record", snapshot.record)
        return payload
    except BaseException as exc:
        if published and not _restore_existing_run_state(mutation, mutation._state_bytes):
            raise StateError("cannot restore existing run ledger after failed write") from exc
        if isinstance(exc, OSError):
            raise StateError("cannot durably write existing run ledger") from None
        raise
    finally:
        if temporary_fd is not None:
            _close_noerror(temporary_fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=mutation._run_fd)
            except OSError:
                pass


def _restore_existing_run_state(mutation: ExistingRunMutation, content: bytes) -> bool:
    """Best-effort visible rollback when a post-publication durability check fails."""
    temporary = f".state-rollback-{uuid.uuid4().hex}"
    temporary_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        temporary_fd = os.open(temporary, flags, 0o600, dir_fd=mutation._run_fd)
        os.fchmod(temporary_fd, 0o600)
        _write_all(temporary_fd, content)
        try:
            os.fsync(temporary_fd)
        except OSError:
            pass
        os.replace(temporary, "state.json", src_dir_fd=mutation._run_fd, dst_dir_fd=mutation._run_fd)
        temporary = ""
        try:
            os.fsync(mutation._run_fd)
        except OSError:
            pass
        return _read_exact_private_file(mutation._run_fd, "state.json") == content
    except BaseException:
        return False
    finally:
        if temporary_fd is not None:
            _close_noerror(temporary_fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=mutation._run_fd)
            except OSError:
                pass


def _validate_pinned_tree_contents(directory_fd: int) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError:
        raise StateError("cannot securely delete existing run") from None
    for name in names:
        if not isinstance(name, str) or name in {".", ".."} or "/" in name or "\x00" in name:
            raise StateError("cannot securely delete existing run")
        entry = _safe_stat(name, directory_fd=directory_fd)
        if entry is None:
            raise StateError("existing run changed during deletion")
        if stat_module.S_ISDIR(entry.st_mode):
            child_fd = _open_readonly_no_follow(name, directory_fd=directory_fd, directory=True)
            if child_fd is None:
                raise StateError("cannot securely delete existing run")
            try:
                opened = _safe_fstat(child_fd)
                if opened is None or _identity(opened) != _identity(entry):
                    raise StateError("existing run changed during deletion")
                _validate_pinned_tree_contents(child_fd)
            finally:
                _close_noerror(child_fd)
        elif stat_module.S_ISREG(entry.st_mode) or stat_module.S_ISLNK(entry.st_mode):
            continue
        else:
            raise StateError("existing run contains an unsupported filesystem entry")


def _restore_renamed_entry(directory_fd: int, tombstone: str, name: str) -> None:
    if _safe_stat(name, directory_fd=directory_fd) is not None:
        return
    try:
        os.rename(tombstone, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except OSError:
        pass


def _open_run_deletions_directory(roots: StatePaths, *, canonical_state: Path | None = None) -> int:
    state_root = roots.state.resolve() if canonical_state is None else canonical_state
    state_fd = _open_readonly_no_follow(state_root, directory=True)
    if state_fd is None:
        raise StateError("cannot securely open run deletion quarantine")
    deletions_fd: int | None = None
    try:
        state_open = _safe_fstat(state_fd)
        state_path = _safe_stat(state_root)
        if (
            state_open is None
            or state_path is None
            or not stat_module.S_ISDIR(state_open.st_mode)
            or not stat_module.S_ISDIR(state_path.st_mode)
            or _identity(state_open) != _identity(state_path)
        ):
            raise StateError("cannot securely open run deletion quarantine")
        entry = _safe_stat("run-deletions", directory_fd=state_fd)
        if entry is None:
            try:
                os.mkdir("run-deletions", 0o700, dir_fd=state_fd)
                os.fsync(state_fd)
            except OSError:
                raise StateError("cannot create run deletion quarantine") from None
            entry = _safe_stat("run-deletions", directory_fd=state_fd)
        deletions_fd = _open_readonly_no_follow("run-deletions", directory_fd=state_fd, directory=True)
        opened = None if deletions_fd is None else _safe_fstat(deletions_fd)
        if (
            entry is None
            or deletions_fd is None
            or opened is None
            or not stat_module.S_ISDIR(entry.st_mode)
            or not stat_module.S_ISDIR(opened.st_mode)
            or entry.st_uid != os.geteuid()
            or opened.st_uid != os.geteuid()
            or _identity(entry) != _identity(opened)
        ):
            raise StateError("cannot securely open run deletion quarantine")
        try:
            os.fchmod(deletions_fd, 0o700)
        except OSError:
            raise StateError("cannot secure run deletion quarantine") from None
        result = deletions_fd
        deletions_fd = None
        return result
    finally:
        if deletions_fd is not None:
            _close_noerror(deletions_fd)
        _close_noerror(state_fd)


def _restore_quarantined_run(
    runs_fd: int,
    deletions_fd: int,
    tombstone: str,
    name: str,
) -> None:
    if _safe_stat(name, directory_fd=runs_fd) is not None:
        return
    try:
        os.rename(tombstone, name, src_dir_fd=deletions_fd, dst_dir_fd=runs_fd)
    except OSError:
        return
    for directory_fd in (deletions_fd, runs_fd):
        try:
            os.fsync(directory_fd)
        except OSError:
            pass


def _retry_run_deletion_quarantines(roots: StatePaths) -> None:
    """Best-effort cleanup for run trees whose logical deletion already committed."""
    deletions_fd: int | None = None
    try:
        deletions_fd = _open_run_deletions_directory(roots)
        names = sorted(os.listdir(deletions_fd))
    except (OSError, StateError):
        if deletions_fd is not None:
            _close_noerror(deletions_fd)
        return
    try:
        for name in names:
            run_name, separator, nonce = name.rpartition("-")
            if (
                not separator
                or _NAME_RE.fullmatch(run_name) is None
                or len(nonce) != 32
                or any(character not in "0123456789abcdef" for character in nonce)
            ):
                continue
            try:
                with _new_run_name_lock(roots, run_name):
                    entry = _safe_stat(name, directory_fd=deletions_fd)
                    if entry is None or not stat_module.S_ISDIR(entry.st_mode) or entry.st_uid != os.geteuid():
                        continue
                    run_fd = _open_readonly_no_follow(name, directory_fd=deletions_fd, directory=True)
                    if run_fd is None:
                        continue
                    try:
                        opened = _safe_fstat(run_fd)
                        if opened is None or _identity(opened) != _identity(entry):
                            continue
                        _validate_pinned_tree_contents(run_fd)
                        _remove_pinned_tree_contents(run_fd)
                        os.fsync(run_fd)
                    except (OSError, StateError):
                        continue
                    finally:
                        _close_noerror(run_fd)
                    current = _safe_stat(name, directory_fd=deletions_fd)
                    if current is None or _identity(current) != _identity(entry):
                        continue
                    try:
                        os.rmdir(name, dir_fd=deletions_fd)
                        os.fsync(deletions_fd)
                    except OSError:
                        continue
            except StateError:
                continue
    finally:
        _close_noerror(deletions_fd)


def _remove_pinned_tree_contents(directory_fd: int) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError:
        raise StateError("cannot securely delete existing run") from None
    for name in names:
        entry = _safe_stat(name, directory_fd=directory_fd)
        if entry is None:
            raise StateError("existing run changed during deletion")
        tombstone = f".deleting-{uuid.uuid4().hex}"
        try:
            os.rename(name, tombstone, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except OSError:
            raise StateError("cannot securely delete existing run") from None
        moved = _safe_stat(tombstone, directory_fd=directory_fd)
        if moved is None or _identity(moved) != _identity(entry):
            _restore_renamed_entry(directory_fd, tombstone, name)
            raise StateError("existing run changed during deletion")
        if stat_module.S_ISDIR(entry.st_mode):
            child_fd = _open_readonly_no_follow(tombstone, directory_fd=directory_fd, directory=True)
            if child_fd is None:
                _restore_renamed_entry(directory_fd, tombstone, name)
                raise StateError("cannot securely delete existing run")
            try:
                opened = _safe_fstat(child_fd)
                if opened is None or _identity(opened) != _identity(entry):
                    raise StateError("existing run changed during deletion")
                _remove_pinned_tree_contents(child_fd)
                os.fsync(child_fd)
            finally:
                _close_noerror(child_fd)
            try:
                os.rmdir(tombstone, dir_fd=directory_fd)
            except OSError:
                raise StateError("cannot securely delete existing run") from None
        elif stat_module.S_ISREG(entry.st_mode) or stat_module.S_ISLNK(entry.st_mode):
            try:
                os.unlink(tombstone, dir_fd=directory_fd)
            except OSError:
                raise StateError("cannot securely delete existing run") from None
        else:
            _restore_renamed_entry(directory_fd, tombstone, name)
            raise StateError("existing run contains an unsupported filesystem entry")


def _delete_existing_run_tree(mutation: ExistingRunMutation) -> None:
    mutation.verify_binding()
    _validate_pinned_tree_contents(mutation._run_fd)
    mutation.verify_binding()
    tombstone = f"{mutation.paths.name}-{uuid.uuid4().hex}"
    deletions_fd = _open_run_deletions_directory(
        mutation._roots,
        canonical_state=mutation.paths.root.parent.parent,
    )
    try:
        try:
            os.rename(
                mutation.paths.name,
                tombstone,
                src_dir_fd=mutation._runs_fd,
                dst_dir_fd=deletions_fd,
            )
        except OSError:
            raise StateError("cannot quarantine existing run for deletion") from None
        quarantined = _safe_stat(tombstone, directory_fd=deletions_fd)
        pinned = _safe_fstat(mutation._run_fd)
        if (
            quarantined is None
            or pinned is None
            or _identity(quarantined) != mutation._run_identity
            or _identity(pinned) != mutation._run_identity
        ):
            _restore_quarantined_run(
                mutation._runs_fd,
                deletions_fd,
                tombstone,
                mutation.paths.name,
            )
            raise StateError("existing run changed during deletion")
        try:
            os.fsync(deletions_fd)
            os.fsync(mutation._runs_fd)
        except OSError:
            _restore_quarantined_run(
                mutation._runs_fd,
                deletions_fd,
                tombstone,
                mutation.paths.name,
            )
            raise StateError("cannot durably quarantine existing run for deletion") from None
        _remove_pinned_tree_contents(mutation._run_fd)
        try:
            os.fsync(mutation._run_fd)
        except OSError:
            raise StateError("cannot durably delete existing run") from None
        current = _safe_stat(tombstone, directory_fd=deletions_fd)
        pinned = _safe_fstat(mutation._run_fd)
        if (
            current is None
            or pinned is None
            or _identity(current) != mutation._run_identity
            or _identity(pinned) != mutation._run_identity
        ):
            raise StateError("existing run changed during deletion")
        try:
            os.rmdir(tombstone, dir_fd=deletions_fd)
            os.fsync(deletions_fd)
        except OSError:
            raise StateError("cannot durably delete existing run") from None
        object.__setattr__(mutation, "_deleted", True)
    finally:
        _close_noerror(deletions_fd)


@contextmanager
def locked_existing_run(
    roots: StatePaths,
    name: str,
    *,
    expected: ExistingRunRecord | None = None,
    expected_snapshot: RunLedgerSnapshot | None = None,
    lock_timeout: float | None = None,
) -> Iterator[ExistingRunMutation]:
    """Lock first, then pin and re-read one existing run mutation authority."""
    paths = _new_run_paths(roots, name)
    with _new_run_name_lock(roots, name, lock_timeout=lock_timeout) as name_lock:
        runs_fd = _open_readonly_no_follow(roots.runs, directory=True)
        if runs_fd is None:
            raise StateError("cannot securely open existing runs")
        run_fd: int | None = None
        try:
            runs_open = _safe_fstat(runs_fd)
            runs_path = _safe_stat(roots.runs)
            entry = _safe_stat(name, directory_fd=runs_fd)
            run_fd = _open_readonly_no_follow(name, directory_fd=runs_fd, directory=True)
            run_open = None if run_fd is None else _safe_fstat(run_fd)
            if (
                runs_open is None
                or runs_path is None
                or entry is None
                or run_fd is None
                or run_open is None
                or not stat_module.S_ISDIR(runs_open.st_mode)
                or not stat_module.S_ISDIR(runs_path.st_mode)
                or not stat_module.S_ISDIR(entry.st_mode)
                or not stat_module.S_ISDIR(run_open.st_mode)
                or _identity(runs_open) != _identity(runs_path)
                or _identity(entry) != _identity(run_open)
            ):
                raise StateError("cannot securely open existing run")
            owner_bytes = _read_exact_private_file(run_fd, "owner.json")
            state_bytes = _read_exact_private_file(run_fd, "state.json")
            raw_owner = _decode_exact_json_object(owner_bytes)
            raw_state = _decode_exact_json_object(state_bytes)
            snapshot = _snapshot_from_payloads(name, raw_owner, raw_state)
            if expected is not None and snapshot.record != expected:
                raise StateError("run ledger changed before lifecycle mutation")
            if expected_snapshot is not None and (
                not isinstance(expected_snapshot, RunLedgerSnapshot)
                or snapshot.record != expected_snapshot.record
                or snapshot.state != expected_snapshot.state
            ):
                raise StateError("run ledger changed before lifecycle mutation")
            mutation = ExistingRunMutation(
                roots,
                paths,
                snapshot,
                owner_bytes,
                state_bytes,
                runs_fd,
                run_fd,
                name_lock,
                _identity(runs_open),
                _identity(run_open),
            )
            mutation.verify_binding()
            yield mutation
        finally:
            if run_fd is not None:
                _close_noerror(run_fd)
            _close_noerror(runs_fd)


def run_entry_present_or_ambiguous(roots: StatePaths, name: str) -> bool:
    """Return false only when a run entry is securely proven absent.

    The configured state root is the trust anchor.  Its ``runs`` child and the
    named entry are inspected without following either symlinks or path swaps.
    Every non-ENOENT outcome is present or ambiguous and must be validated by
    the durable run dispatcher rather than treated as a free name.
    """
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise StateError("invalid run name")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    runs_fd: int | None = None
    runs_missing = False
    runs_open_failed = False
    try:
        runs_fd = os.open(roots.runs, flags)
    except FileNotFoundError:
        runs_missing = True
    except OSError:
        runs_open_failed = True
    if runs_missing:
        return False
    if runs_open_failed or runs_fd is None:
        raise StateError("cannot securely inspect run entry")

    try:
        runs_before = _safe_fstat(runs_fd)
        entry_missing = False
        entry_stat_failed = False
        entry: os.stat_result | None = None
        try:
            entry = os.stat(name, dir_fd=runs_fd, follow_symlinks=False)
        except FileNotFoundError:
            entry_missing = True
        except OSError:
            entry_stat_failed = True
        runs_after = _safe_fstat(runs_fd)
        current_runs = _safe_stat(roots.runs)
        if (
            runs_before is None
            or runs_after is None
            or current_runs is None
            or entry_stat_failed
            or (entry is None and not entry_missing)
            or not stat_module.S_ISDIR(runs_before.st_mode)
            or not stat_module.S_ISDIR(runs_after.st_mode)
            or not stat_module.S_ISDIR(current_runs.st_mode)
            or (runs_after.st_dev, runs_after.st_ino, runs_after.st_ctime_ns)
            != (runs_before.st_dev, runs_before.st_ino, runs_before.st_ctime_ns)
            or (current_runs.st_dev, current_runs.st_ino) != (runs_before.st_dev, runs_before.st_ino)
        ):
            raise StateError("cannot securely inspect run entry")
        if entry_missing:
            return False
        return True
    finally:
        _close_noerror(runs_fd)


def _read_pinned_json_object_snapshot(
    directory_fd: int, filename: str
) -> tuple[dict[str, Any], tuple[int, int, int, int, int]]:
    """Read one bounded regular JSON file relative to an already pinned directory."""
    pre_open = _safe_stat(filename, directory_fd=directory_fd)
    if pre_open is None or not stat_module.S_ISREG(pre_open.st_mode) or pre_open.st_size > _MAX_RUN_LEDGER_BYTES:
        raise StateError("cannot securely read run ledger")
    # O_NONBLOCK is immaterial for a regular file but prevents a path swap to a
    # FIFO or device-like node from hanging this validation boundary.  The
    # post-open identity/type check rejects any such replacement before reads.
    file_fd = _open_readonly_no_follow(filename, directory_fd=directory_fd, nonblocking=True)
    if file_fd is None:
        raise StateError("cannot securely read run ledger")
    try:
        before = _safe_fstat(file_fd)
        if (
            before is None
            or not stat_module.S_ISREG(before.st_mode)
            or before.st_size > _MAX_RUN_LEDGER_BYTES
            or (before.st_dev, before.st_ino, stat_module.S_IFMT(before.st_mode))
            != (pre_open.st_dev, pre_open.st_ino, stat_module.S_IFMT(pre_open.st_mode))
        ):
            raise StateError("cannot securely read run ledger")
        content = bytearray()
        read_failed = False
        while True:
            try:
                chunk = os.read(file_fd, min(64 * 1024, _MAX_RUN_LEDGER_BYTES + 1 - len(content)))
            except OSError:
                read_failed = True
                break
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > _MAX_RUN_LEDGER_BYTES:
                raise StateError("cannot securely read run ledger")
        if read_failed:
            raise StateError("cannot securely read run ledger")
        after = _safe_fstat(file_fd)
        if (
            after is None
            or not stat_module.S_ISREG(after.st_mode)
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        ):
            raise StateError("run ledger changed during read")
        current = _safe_stat(filename, directory_fd=directory_fd)
        if (
            current is None
            or not stat_module.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (
                before.st_dev,
                before.st_ino,
            )
        ):
            raise StateError("run ledger changed during read")
        decoded: str | None = None
        try:
            decoded = bytes(content).decode("utf-8")
        except UnicodeDecodeError:
            pass
        if decoded is None:
            raise StateError("cannot securely read run ledger")
        parsed = False
        value: Any = None
        try:
            value = json.loads(decoded)
            parsed = True
        except (RecursionError, ValueError):
            pass
        if not parsed:
            raise StateError("cannot securely read run ledger")
        if not isinstance(value, dict):
            raise StateError("cannot securely read run ledger")
        return value, (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    finally:
        _close_noerror(file_fd)


def _read_pinned_json_object(directory_fd: int, filename: str) -> dict[str, Any]:
    return _read_pinned_json_object_snapshot(directory_fd, filename)[0]


def _read_pinned_run_payloads(
    runs_fd: int,
    name: str,
    *,
    expected_directory: os.stat_result | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    directory_fd = _open_readonly_no_follow(name, directory_fd=runs_fd, directory=True)
    if directory_fd is None:
        raise StateError("cannot securely read run ledger")
    try:
        directory_before = _safe_fstat(directory_fd)
        if directory_before is None:
            raise StateError("cannot securely read run ledger")
        if not stat_module.S_ISDIR(directory_before.st_mode):
            raise StateError("cannot securely read run ledger")
        if expected_directory is not None and (directory_before.st_dev, directory_before.st_ino) != (
            expected_directory.st_dev,
            expected_directory.st_ino,
        ):
            raise StateError("run ledger changed during read")
        raw_owner = _read_pinned_json_object(directory_fd, "owner.json")
        raw_state = _read_pinned_json_object(directory_fd, "state.json")
        directory_after = _safe_fstat(directory_fd)
        current_directory = _safe_stat(name, directory_fd=runs_fd)
        if (
            directory_after is None
            or current_directory is None
            or not stat_module.S_ISDIR(directory_after.st_mode)
            or not stat_module.S_ISDIR(current_directory.st_mode)
            or (directory_after.st_dev, directory_after.st_ino, directory_after.st_ctime_ns)
            != (directory_before.st_dev, directory_before.st_ino, directory_before.st_ctime_ns)
            or (current_directory.st_dev, current_directory.st_ino)
            != (directory_before.st_dev, directory_before.st_ino)
        ):
            raise StateError("run ledger changed during read")
        return raw_owner, raw_state
    finally:
        _close_noerror(directory_fd)


def _normalize_run_dispatch_record(
    name: str,
    raw_owner: dict[str, Any],
    raw_state: dict[str, Any],
) -> ExistingRunRecord:
    if set(raw_owner) != {"schema_version", "run_id", "name"}:
        raise StateError("invalid owner record")
    if type(raw_owner.get("schema_version")) is not int or raw_owner["schema_version"] != 1:
        raise StateError("invalid owner schema")
    owner_name = raw_owner.get("name")
    if not isinstance(owner_name, str) or _NAME_RE.fullmatch(owner_name) is None or owner_name != name:
        raise StateError("invalid owner identity")
    run_id = raw_owner.get("run_id")
    if not isinstance(run_id, str):
        raise StateError("invalid owner identity")
    parsed_run_id: uuid.UUID | None = None
    try:
        parsed_run_id = uuid.UUID(run_id)
    except (AttributeError, TypeError, ValueError):
        pass
    if parsed_run_id is None or str(parsed_run_id) != run_id:
        raise StateError("invalid owner identity")

    raw_schema = raw_state.get("schema_version", 1)
    if type(raw_schema) is not int or raw_schema not in {1, 2}:
        raise StateError("invalid run state schema")
    lifecycle_revision(raw_state)

    if raw_schema == 1:
        raw_kind = raw_state.get("runtime_kind", RuntimeKind.CLOUD_IMAGE.value)
        if raw_kind != RuntimeKind.CLOUD_IMAGE.value:
            raise StateError("invalid run state runtime kind")
        raw_backend = raw_state.get("backend", RuntimeBackend.KVM.value)
    else:
        if not {"runtime_kind", "backend", "name", "run_id"}.issubset(raw_state):
            raise StateError("invalid run state schema 2 identity")
        raw_kind = raw_state["runtime_kind"]
        raw_backend = raw_state["backend"]

    runtime_kind: RuntimeKind | None = None
    try:
        runtime_kind = RuntimeKind(raw_kind)
    except (TypeError, ValueError):
        pass
    if runtime_kind is None:
        raise StateError("invalid run state runtime kind")
    backend: RuntimeBackend | None = None
    try:
        backend = RuntimeBackend(raw_backend)
    except (TypeError, ValueError):
        pass
    if backend is None:
        raise StateError("invalid run state runtime backend")
    dispatch_key: DispatchKey | None = None
    try:
        dispatch_key = DispatchKey(runtime_kind, backend)
    except (TypeError, ValueError):
        pass
    if dispatch_key is None:
        raise StateError("invalid runtime/backend combination")

    if "run_id" in raw_state and (not isinstance(raw_state["run_id"], str) or raw_state["run_id"] != run_id):
        raise StateError("state run_id mismatch")
    if "name" in raw_state and (not isinstance(raw_state["name"], str) or raw_state["name"] != owner_name):
        raise StateError("state name mismatch")

    normalized: ExistingRunRecord | None = None
    try:
        normalized = ExistingRunRecord(
            name=owner_name,
            run_id=run_id,
            state_schema_version=raw_schema,
            dispatch_key=dispatch_key,
        )
    except (TypeError, ValueError):
        pass
    if normalized is None:
        raise StateError("invalid normalized run record")
    return normalized


def _verify_pinned_runs_root(roots: StatePaths, runs_fd: int, before: os.stat_result) -> None:
    after = _safe_fstat(runs_fd)
    current = _safe_stat(roots.runs)
    if (
        after is None
        or current is None
        or not stat_module.S_ISDIR(after.st_mode)
        or not stat_module.S_ISDIR(current.st_mode)
        or (after.st_dev, after.st_ino, after.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_ctime_ns)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise StateError("run ledger changed during read")


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    if value is None or type(value) in {bool, int, float, str}:
        return value
    raise StateError("invalid run ledger value")


def _snapshot_from_payloads(
    name: str,
    raw_owner: dict[str, Any],
    raw_state: dict[str, Any],
) -> RunLedgerSnapshot:
    record = _normalize_run_dispatch_record(name, raw_owner, raw_state)
    status = raw_state.get("status")
    if not isinstance(status, str) or status not in ALLOWED_RUNTIME_STATUSES[record.dispatch_key.runtime_kind]:
        raise StateError("invalid run status")
    _reject_secrets(raw_state)
    frozen = _freeze_json_value(deepcopy(raw_state))
    if not isinstance(frozen, Mapping):
        raise StateError("invalid run ledger")
    return RunLedgerSnapshot(record, frozen)


def lifecycle_revision(value: RunLedgerSnapshot | Mapping[str, Any]) -> int:
    """Return the validated durable revision, defaulting legacy ledgers to zero."""

    state_value = value.state if isinstance(value, RunLedgerSnapshot) else value
    if not isinstance(state_value, Mapping):
        raise StateError("invalid lifecycle revision")
    revision = state_value.get("lifecycle_revision", 0)
    if type(revision) is not int or revision < 0 or revision > _MAX_LIFECYCLE_REVISION:
        raise StateError("invalid lifecycle revision")
    return revision


def snapshot_from_runtime_observation(
    expected: ExistingRunRecord,
    observed_state: Mapping[str, Any],
) -> RunLedgerSnapshot:
    """Validate an adapter's in-memory observation against immutable run identity."""
    if not isinstance(expected, ExistingRunRecord) or not isinstance(observed_state, Mapping):
        raise StateError("invalid runtime observation")
    owner = {
        "schema_version": 1,
        "run_id": expected.run_id,
        "name": expected.name,
    }
    snapshot = _snapshot_from_payloads(expected.name, owner, deepcopy(dict(observed_state)))
    if snapshot.record != expected:
        raise StateError("runtime observation changed durable identity")
    return snapshot


def read_run_dispatch_record(roots: StatePaths, name: str) -> ExistingRunRecord:
    """Read and normalize only the durable identity fields used for dispatch.

    This reader is intentionally side-effect-free.  Legacy mutable state is
    normalized in memory and is never rewritten by inspection or routing.
    """
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise StateError("invalid run name")
    runs_fd = _open_readonly_no_follow(roots.runs, directory=True)
    if runs_fd is None:
        raise StateError("cannot securely read run ledger")
    try:
        runs_before = _safe_fstat(runs_fd)
        if runs_before is None or not stat_module.S_ISDIR(runs_before.st_mode):
            raise StateError("cannot securely read run ledger")
        raw_owner, raw_state = _read_pinned_run_payloads(runs_fd, name)
        _verify_pinned_runs_root(roots, runs_fd, runs_before)
    finally:
        _close_noerror(runs_fd)
    return _normalize_run_dispatch_record(name, raw_owner, raw_state)


def read_run_ledger_snapshot(roots: StatePaths, name: str) -> RunLedgerSnapshot:
    """Securely read one complete run ledger for internal aggregation use."""
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise StateError("invalid run name")
    runs_fd = _open_readonly_no_follow(roots.runs, directory=True)
    if runs_fd is None:
        raise StateError("cannot securely read run ledger")
    try:
        runs_before = _safe_fstat(runs_fd)
        if runs_before is None or not stat_module.S_ISDIR(runs_before.st_mode):
            raise StateError("cannot securely read run ledger")
        raw_owner, raw_state = _read_pinned_run_payloads(runs_fd, name)
        snapshot = _snapshot_from_payloads(name, raw_owner, raw_state)
        _verify_pinned_runs_root(roots, runs_fd, runs_before)
        return snapshot
    except (RecursionError, TypeError, ValueError):
        raise StateError("invalid run ledger") from None
    finally:
        _close_noerror(runs_fd)


def enumerate_run_snapshots(
    roots: StatePaths,
) -> tuple[tuple[RunLedgerSnapshot, ...], tuple[RunAggregationError, ...]]:
    """Return deterministic pinned ledger payloads for later safe projection."""
    try:
        root_stat = os.stat(roots.runs, follow_symlinks=False)
    except FileNotFoundError:
        return (), ()
    except OSError:
        raise StateError("cannot securely enumerate run ledgers") from None
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise StateError("cannot securely enumerate run ledgers")
    runs_fd = _open_readonly_no_follow(roots.runs, directory=True)
    if runs_fd is None:
        raise StateError("cannot securely enumerate run ledgers")
    snapshots: list[RunLedgerSnapshot] = []
    errors: list[RunAggregationError] = []
    try:
        runs_before = _safe_fstat(runs_fd)
        if (
            runs_before is None
            or not stat_module.S_ISDIR(runs_before.st_mode)
            or (runs_before.st_dev, runs_before.st_ino) != (root_stat.st_dev, root_stat.st_ino)
        ):
            raise StateError("cannot securely enumerate run ledgers")
        try:
            names = sorted(os.listdir(runs_fd))
        except OSError:
            raise StateError("cannot securely enumerate run ledgers") from None
        for raw_name in names:
            if not isinstance(raw_name, str) or _NAME_RE.fullmatch(raw_name) is None:
                entry_digest = hashlib.sha256(raw_name.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
                errors.append(
                    RunAggregationError(
                        None,
                        f"entry-{entry_digest}",
                        RuntimeOperation.PS,
                        None,
                        "invalid-entry",
                        "invalid run entry",
                    )
                )
                continue
            entry_stat = _safe_stat(raw_name, directory_fd=runs_fd)
            if entry_stat is None or not stat_module.S_ISDIR(entry_stat.st_mode):
                errors.append(
                    RunAggregationError(
                        raw_name,
                        None,
                        RuntimeOperation.PS,
                        None,
                        "invalid-entry",
                        "invalid run entry",
                    )
                )
                continue
            record: ExistingRunRecord | None = None
            try:
                raw_owner, raw_state = _read_pinned_run_payloads(
                    runs_fd,
                    raw_name,
                    expected_directory=entry_stat,
                )
                record = _normalize_run_dispatch_record(raw_name, raw_owner, raw_state)
                snapshots.append(_snapshot_from_payloads(raw_name, raw_owner, raw_state))
            except (RecursionError, StateError, TypeError, ValueError):
                errors.append(
                    RunAggregationError(
                        raw_name,
                        None,
                        RuntimeOperation.PS,
                        None if record is None else record.dispatch_key,
                        "invalid-ledger",
                        "invalid run ledger",
                    )
                )
        _verify_pinned_runs_root(roots, runs_fd, runs_before)
    finally:
        _close_noerror(runs_fd)
    return tuple(snapshots), tuple(errors)


def require_bound_run_dispatch_record(roots: StatePaths, expected: ExistingRunRecord) -> None:
    """Revalidate a dispatch binding at an adapter's side-effect boundary."""
    current = read_run_dispatch_record(roots, expected.name)
    if current != expected:
        raise StateError("run ledger changed during adapter entry")
    # This guard binds all cooperative Palimpsest adapter side effects to the
    # selected record.  The OS cannot freeze files against an arbitrary
    # same-owner raw writer after this point, so backend ownership checks and
    # lifecycle locks remain mandatory inside each adapter.


@dataclass(slots=True)
class PinnedRunConsole:
    """Fixed-purpose, descriptor-pinned authority for one retained console."""

    roots: StatePaths
    record: ExistingRunRecord
    runs_fd: int
    run_fd: int
    owner_fd: int
    state_fd: int
    console_fd: int
    runs_metadata: os.stat_result
    run_metadata: os.stat_result
    console_metadata: os.stat_result
    initial_size: int
    position: int = 0
    closed: bool = False

    def _fail(self, category: LogErrorCategory) -> None:
        raise LogStreamError(category)

    def _opened_console_size(self, *, allow_unlinked: bool = False) -> int:
        if self.closed:
            self._fail(LogErrorCategory.READ_FAILED)
        opened = _safe_fstat(self.console_fd)
        if (
            opened is None
            or not stat_module.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink not in ({0, 1} if allow_unlinked else {1})
            or stat_module.S_IMODE(opened.st_mode) != 0o600
            or _identity(opened) != _identity(self.console_metadata)
            or opened.st_size < self.initial_size
            or opened.st_size < self.position
        ):
            self._fail(LogErrorCategory.CONSOLE_CHANGED)
        return opened.st_size

    def _console_size(self, *, allow_unlinked: bool = False) -> int:
        size = self._opened_console_size(allow_unlinked=allow_unlinked)
        current = _safe_stat("console.log", directory_fd=self.run_fd)
        if current is None and allow_unlinked:
            return size
        if (
            current is None
            or not stat_module.S_ISREG(current.st_mode)
            or _identity(current) != _identity(self.console_metadata)
        ):
            self._fail(LogErrorCategory.CONSOLE_CHANGED)
        return size

    def read(self, maximum: int, *, snapshot: bool = False) -> bytes:
        if type(maximum) is not int or not 1 <= maximum <= 64 * 1024:
            raise ValueError("console reads must be between 1 and 65536 bytes")
        size = self._console_size(allow_unlinked=not snapshot)
        boundary = min(size, self.initial_size) if snapshot else size
        remaining = boundary - self.position
        if remaining <= 0:
            return b""
        try:
            content = os.pread(self.console_fd, min(maximum, remaining), self.position)
        except OSError:
            self._fail(LogErrorCategory.READ_FAILED)
        if not content:
            self._fail(LogErrorCategory.CONSOLE_CHANGED)
        self.position += len(content)
        return content

    def current_status(self) -> str:
        """Revalidate the exact run and console binding at a follow EOF."""
        runs_current = _safe_stat(self.roots.runs)
        if (
            runs_current is None
            or not stat_module.S_ISDIR(runs_current.st_mode)
            or runs_current.st_uid != os.geteuid()
            or stat_module.S_IMODE(runs_current.st_mode) != 0o700
            or _identity(runs_current) != _identity(self.runs_metadata)
        ):
            self._fail(LogErrorCategory.RUN_CHANGED)
        try:
            run_current = os.stat(self.record.name, dir_fd=self.runs_fd, follow_symlinks=False)
        except FileNotFoundError:
            self._opened_console_size(allow_unlinked=True)
            return "removed"
        except OSError:
            self._fail(LogErrorCategory.RUN_CHANGED)
        if (
            not stat_module.S_ISDIR(run_current.st_mode)
            or run_current.st_uid != os.geteuid()
            or stat_module.S_IMODE(run_current.st_mode) != 0o700
            or _identity(run_current) != _identity(self.run_metadata)
        ):
            self._fail(LogErrorCategory.RUN_CHANGED)
        self._console_size()
        try:
            raw_owner = _read_pinned_json_object(self.run_fd, "owner.json")
            raw_state = _read_pinned_json_object(self.run_fd, "state.json")
            current = _normalize_run_dispatch_record(self.record.name, raw_owner, raw_state)
            status = raw_state.get("status")
        except (RecursionError, StateError, TypeError, ValueError):
            self._fail(LogErrorCategory.RUN_CHANGED)
        if (
            current != self.record
            or not isinstance(status, str)
            or status not in ALLOWED_RUNTIME_STATUSES[self.record.dispatch_key.runtime_kind]
        ):
            self._fail(LogErrorCategory.RUN_CHANGED)
        return status

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for descriptor in (self.console_fd, self.state_fd, self.owner_fd, self.run_fd, self.runs_fd):
            _close_noerror(descriptor)


def _open_pinned_private_file(directory_fd: int, filename: str) -> tuple[int, bytes]:
    before = _safe_stat(filename, directory_fd=directory_fd)
    if (
        before is None
        or not stat_module.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat_module.S_IMODE(before.st_mode) != 0o600
        or before.st_size > _MAX_RUN_LEDGER_BYTES
    ):
        raise StateError("cannot securely pin run file")
    descriptor = _open_readonly_no_follow(filename, directory_fd=directory_fd, nonblocking=True)
    if descriptor is None:
        raise StateError("cannot securely pin run file")
    try:
        opened = _safe_fstat(descriptor)
        if opened is None or _identity(opened) != _identity(before):
            raise StateError("cannot securely pin run file")
        content = bytearray()
        while len(content) <= _MAX_RUN_LEDGER_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_RUN_LEDGER_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_RUN_LEDGER_BYTES:
            raise StateError("cannot securely pin run file")
        after = _safe_fstat(descriptor)
        current = _safe_stat(filename, directory_fd=directory_fd)
        if (
            after is None
            or current is None
            or _identity(after) != _identity(before)
            or _identity(current) != _identity(before)
            or len(content) != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise StateError("cannot securely pin run file")
        return descriptor, bytes(content)
    except Exception:
        _close_noerror(descriptor)
        raise


def open_pinned_run_console(roots: StatePaths, expected: ExistingRunRecord) -> PinnedRunConsole:
    """Pin one exact owner/state/console chain without following special files."""
    descriptors: list[int] = []
    try:
        runs_fd = _open_readonly_no_follow(roots.runs, directory=True)
        if runs_fd is None:
            raise LogStreamError(LogErrorCategory.RUN_CHANGED)
        descriptors.append(runs_fd)
        runs_metadata = _safe_fstat(runs_fd)
        run_fd = _open_readonly_no_follow(expected.name, directory_fd=runs_fd, directory=True)
        if runs_metadata is None or run_fd is None:
            raise LogStreamError(LogErrorCategory.RUN_CHANGED)
        descriptors.append(run_fd)
        run_metadata = _safe_fstat(run_fd)
        current_runs = _safe_stat(roots.runs)
        current_run = _safe_stat(expected.name, directory_fd=runs_fd)
        if (
            runs_metadata is None
            or current_runs is None
            or current_run is None
            or not stat_module.S_ISDIR(runs_metadata.st_mode)
            or not stat_module.S_ISDIR(current_runs.st_mode)
            or runs_metadata.st_uid != os.geteuid()
            or stat_module.S_IMODE(runs_metadata.st_mode) != 0o700
            or _identity(current_runs) != _identity(runs_metadata)
            or run_metadata is None
            or not stat_module.S_ISDIR(run_metadata.st_mode)
            or run_metadata.st_uid != os.geteuid()
            or stat_module.S_IMODE(run_metadata.st_mode) != 0o700
            or _identity(current_run) != _identity(run_metadata)
        ):
            raise LogStreamError(LogErrorCategory.RUN_CHANGED)
        owner_fd, owner_bytes = _open_pinned_private_file(run_fd, "owner.json")
        descriptors.append(owner_fd)
        state_fd, state_bytes = _open_pinned_private_file(run_fd, "state.json")
        descriptors.append(state_fd)
        try:
            raw_owner = json.loads(owner_bytes)
            raw_state = json.loads(state_bytes)
            if not isinstance(raw_owner, dict) or not isinstance(raw_state, dict):
                raise ValueError("run ledger is not an object")
            current = _normalize_run_dispatch_record(expected.name, raw_owner, raw_state)
        except (RecursionError, TypeError, ValueError):
            raise LogStreamError(LogErrorCategory.RUN_CHANGED) from None
        if current != expected:
            raise LogStreamError(LogErrorCategory.RUN_CHANGED)

        console_before = _safe_stat("console.log", directory_fd=run_fd)
        console_fd = _open_readonly_no_follow("console.log", directory_fd=run_fd, nonblocking=True)
        if console_before is None or console_fd is None:
            raise LogStreamError(LogErrorCategory.INVALID_CONSOLE)
        descriptors.append(console_fd)
        console_opened = _safe_fstat(console_fd)
        if (
            console_opened is None
            or not stat_module.S_ISREG(console_before.st_mode)
            or not stat_module.S_ISREG(console_opened.st_mode)
            or console_opened.st_uid != os.geteuid()
            or console_opened.st_nlink != 1
            or stat_module.S_IMODE(console_opened.st_mode) != 0o600
            or _identity(console_opened) != _identity(console_before)
        ):
            raise LogStreamError(LogErrorCategory.INVALID_CONSOLE)
        return PinnedRunConsole(
            roots,
            expected,
            runs_fd,
            run_fd,
            owner_fd,
            state_fd,
            console_fd,
            runs_metadata,
            run_metadata,
            console_opened,
            console_opened.st_size,
        )
    except LogStreamError:
        for descriptor in reversed(descriptors):
            _close_noerror(descriptor)
        raise
    except (OSError, StateError):
        for descriptor in reversed(descriptors):
            _close_noerror(descriptor)
        raise LogStreamError(LogErrorCategory.RUN_CHANGED) from None


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold one owner-only advisory process lock for the duration of the context."""
    lock_path = path.expanduser().resolve(strict=False)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def artifact_reference_guard(roots: StatePaths) -> Iterator[None]:
    """Serialize artifact-reference commits with physical removal/GC scans."""
    if not isinstance(roots, StatePaths):
        raise StateError("invalid artifact reference lock authority")
    with file_lock(roots.locks / "artifact-references-v1.lock"):
        yield


@contextmanager
def locked(rpaths: RunPaths) -> Iterator[None]:
    with file_lock(rpaths.lock):
        yield


def write_run_state(rpaths: RunPaths, *, status: str, data: dict[str, Any]) -> dict[str, Any]:
    if status not in _STATUSES:
        raise StateError("invalid run status")
    payload = {**data, "status": status}
    with file_lock(rpaths.lock.parent / "artifact-references-v1.lock"):
        _validate_run_reference_targets(rpaths.root.parent.parent, payload)
        atomic_write_json(rpaths.state, payload)
    return payload


def read_run_state(rpaths: RunPaths) -> dict[str, Any]:
    return read_json(rpaths.state)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or _TAG_RE.fullmatch(tag) is None:
        raise StateError("invalid tag name")
    return tag


def tag_path(roots: StatePaths, tag: str) -> Path:
    return _contained(roots.tags, roots.tags / f"{validate_tag(tag)}.json")


def write_tag_record(roots: StatePaths, record: TagRecord) -> None:
    target = tag_path(roots, record.tag)
    lock_path = roots.locks / f"tag-{validate_tag(record.tag)}.lock"
    with artifact_reference_guard(roots), file_lock(lock_path):
        if target.exists() and read_tag_record(roots, record.tag).digest != record.digest:
            raise StateError("tag already maps to a different digest")
        normalized = normalize_digest(record.digest)
        blob = roots.store / "blobs" / "sha256" / normalized.split(":", 1)[1]
        try:
            entry = blob.stat(follow_symlinks=False)
        except OSError:
            raise StateError("tag references a missing artifact") from None
        if not stat_module.S_ISREG(entry.st_mode):
            raise StateError("tag references an unsafe artifact")
        atomic_write_json(target, asdict(record))


def read_tag_record(roots: StatePaths, tag: str) -> TagRecord:
    expected_tag = validate_tag(tag)
    try:
        record = TagRecord(**read_json(tag_path(roots, expected_tag)))
    except TypeError as exc:
        raise StateError("invalid tag record") from exc
    if record.tag != expected_tag:
        raise StateError("tag record does not match its filename")
    return record


def read_tag_record_at(directory_fd: int, tag: str) -> TagRecord:
    """Read a filename-bound tag record through an already-pinned tag directory."""
    return read_tag_record_snapshot_at(directory_fd, tag)[0]


def read_tag_record_snapshot_at(directory_fd: int, tag: str) -> tuple[TagRecord, tuple[int, int, int, int, int]]:
    """Read one tag record and return the identity of the exact file read."""
    expected_tag = validate_tag(tag)
    try:
        payload, identity = _read_pinned_json_object_snapshot(directory_fd, f"{expected_tag}.json")
        record = TagRecord(**payload)
    except TypeError as exc:
        raise StateError("invalid tag record") from exc
    if record.tag != expected_tag:
        raise StateError("tag record does not match its filename")
    return record, identity


def _transfer_path(roots: StatePaths, digest: str) -> Path:
    return _contained(roots.transfers, roots.transfers / f"{normalize_digest(digest).split(':', 1)[1]}.json")


def write_transfer_record(roots: StatePaths, record: TransferRecord) -> None:
    atomic_write_json(_transfer_path(roots, record.digest), asdict(record))


def read_transfer_record(roots: StatePaths, digest: str) -> TransferRecord:
    try:
        return TransferRecord(**read_json(_transfer_path(roots, digest)))
    except TypeError as exc:
        raise StateError("invalid transfer record") from exc


def list_transfer_records(roots: StatePaths) -> list[TransferRecord]:
    return [read_transfer_record(roots, f"sha256:{item.stem}") for item in sorted(roots.transfers.glob("*.json"))]


def delete_transfer_record(roots: StatePaths, digest: str) -> None:
    _transfer_path(roots, digest).unlink(missing_ok=True)


# Hub-facing convenience records preserve the exact transfer protocol without exposing keys.
def fingerprint(path: Path) -> str:
    stat = path.resolve().stat()
    return hashlib.sha256(f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()


def read_transfer(digest: str) -> dict[str, Any] | None:
    roots = paths()
    try:
        return asdict(read_transfer_record(roots, digest))
    except StateError:
        return None


def write_transfer(record: dict[str, Any]) -> None:
    roots = paths()
    write_transfer_record(
        roots,
        TransferRecord(
            1,
            record["digest"],
            record["path_fingerprint"],
            record["session_id"],
            record["acknowledged_offset"],
            record.get("updated_at", utc_now_iso()),
        ),
    )


def delete_transfer(digest: str) -> None:
    delete_transfer_record(paths(), digest)
