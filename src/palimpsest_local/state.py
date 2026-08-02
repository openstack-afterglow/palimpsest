"""Owner-only XDG state, atomic ledgers, tags, locks, and transfer checkpoints."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .digest import normalize_digest
from .errors import StateError

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9.+\-]{0,63}$")
_STATUSES = {"creating", "defined", "starting", "running", "stopping", "stopped", "removed", "failed"}


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
class OwnerRecord:
    schema_version: int
    run_id: str
    name: str


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


def init_roots(environment: dict[str, str] | None = None) -> StatePaths:
    env = environment if environment is not None else os.environ
    config = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "palimpsest"
    state = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "palimpsest"
    roots = StatePaths(config, state)
    for directory in (
        roots.config,
        roots.state,
        roots.store,
        roots.runs,
        roots.locks,
        roots.transfers,
        roots.tags,
        roots.builds,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    return roots


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


@contextmanager
def locked(rpaths: RunPaths) -> Iterator[None]:
    rpaths.lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(rpaths.lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_run_state(rpaths: RunPaths, *, status: str, data: dict[str, Any]) -> dict[str, Any]:
    if status not in _STATUSES:
        raise StateError("invalid run status")
    payload = {**data, "status": status}
    atomic_write_json(rpaths.state, payload)
    return payload


def read_run_state(rpaths: RunPaths) -> dict[str, Any]:
    return read_json(rpaths.state)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def validate_tag(tag: str) -> str:
    if _TAG_RE.fullmatch(tag) is None:
        raise StateError("invalid tag name")
    return tag


def tag_path(roots: StatePaths, tag: str) -> Path:
    return _contained(roots.tags, roots.tags / f"{validate_tag(tag)}.json")


def write_tag_record(roots: StatePaths, record: TagRecord) -> None:
    target = tag_path(roots, record.tag)
    if target.exists() and read_tag_record(roots, record.tag).digest != record.digest:
        raise StateError("tag already maps to a different digest")
    atomic_write_json(target, asdict(record))


def read_tag_record(roots: StatePaths, tag: str) -> TagRecord:
    try:
        return TagRecord(**read_json(tag_path(roots, tag)))
    except TypeError as exc:
        raise StateError("invalid tag record") from exc


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
