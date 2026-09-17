"""Fail-open, owner-private host command lifecycle journal."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = "palimpsest.host-command-journal.v1"
_WARNING = (
    "WARNING: Palimpsest host journal is unavailable; recording may be incomplete, but command execution continues."
)
_FILENAME = "commands.jsonl"
_MAX_RECORD = 512
_LOCK_SECONDS = 0.05
_MAX_FILE = 64 * 1024 * 1024
_FAMILIES = frozenset(
    {
        "image",
        "layer",
        "bundle",
        "oci",
        "registry",
        "run",
        "exec",
        "stop",
        "rm",
        "ps",
        "inspect",
        "logs",
        "shell",
        "compose",
        "build",
        "store",
        "docker",
    }
)


def _root() -> tuple[Path | None, bool]:
    override = os.environ.get("PALIMPSEST_LOG_HOME")
    if override is not None:
        path = Path(override) if override and "\0" not in override else None
        return path, path is None or not path.is_absolute()
    return (Path("/var/log/palimpsest"), False) if sys.platform.startswith("linux") else (None, False)


def _metadata_ok(info: os.stat_result, *, directory: bool) -> bool:
    expected = stat.S_IFDIR if directory else stat.S_IFREG
    return (
        stat.S_IFMT(info.st_mode) == expected
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
        and (directory or info.st_nlink == 1)
    )


def _open_directory(root: Path) -> int:
    if not root.is_absolute() or any(part in {"", ".", ".."} for part in root.parts[1:]):
        raise OSError("invalid journal directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_file(directory: int) -> tuple[int, bool]:
    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        return os.open(_FILENAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory), True
    except FileExistsError:
        return os.open(_FILENAME, flags, dir_fd=directory), False


def _append(root: Path, phase: str, family: str, invocation_id: str, result: str | None) -> None:
    directory = _open_directory(root)
    descriptor = -1
    try:
        if not _metadata_ok(os.fstat(directory), directory=True):
            raise OSError("unsafe journal directory")
        descriptor, created = _open_file(directory)
        if created:
            os.fsync(directory)
        if not _metadata_ok(os.fstat(descriptor), directory=False):
            raise OSError("unsafe journal file")
        deadline = time.monotonic() + _LOCK_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("journal lock timeout") from None
                time.sleep(0.002)
        end = os.lseek(descriptor, 0, os.SEEK_END)
        if end > _MAX_FILE:
            raise OSError("journal size limit reached")
        sequence = 1
        if end:
            start = max(0, end - _MAX_RECORD)
            tail = os.pread(descriptor, end - start, start)
            if not tail.endswith(b"\n"):
                raise OSError("journal tail is incomplete")
            lines = tail.splitlines()
            if not lines or (start and len(lines) < 2):
                raise OSError("journal tail is invalid")
            previous = json.loads(lines[-1])
            if previous.get("schema") != _SCHEMA:
                raise OSError("journal schema is invalid")
            previous_sequence = previous["sequence"]
            if type(previous_sequence) is not int or not 1 <= previous_sequence < 2**64 - 1:
                raise OSError("journal sequence is invalid")
            sequence = previous_sequence + 1
        record = {
            "schema": _SCHEMA,
            "sequence": sequence,
            "invocation_id": invocation_id,
            "observed_at": datetime.now(UTC).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "phase": phase,
            "family": family,
        }
        if result is not None:
            record["result"] = result
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        if (
            len(payload) > _MAX_RECORD
            or end + len(payload) > _MAX_FILE
            or os.write(descriptor, payload) != len(payload)
        ):
            raise OSError("journal append failed")
        os.fsync(descriptor)
        os.fsync(directory)
        visible_file = os.stat(_FILENAME, dir_fd=directory, follow_symlinks=False)
        if (visible_file.st_dev, visible_file.st_ino) != (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino):
            raise OSError("journal file changed")
        current_directory = _open_directory(root)
        try:
            if (os.fstat(current_directory).st_dev, os.fstat(current_directory).st_ino) != (
                os.fstat(directory).st_dev,
                os.fstat(directory).st_ino,
            ):
                raise OSError("journal directory changed")
        finally:
            os.close(current_directory)
        if not _metadata_ok(os.fstat(descriptor), directory=False):
            raise OSError("journal file changed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


@dataclass(slots=True)
class CommandJournal:
    family: str
    root: Path | None
    warned: bool = False
    invalid_root: bool = False
    invocation_id: str = ""

    def _record(self, phase: str, result: str | None = None) -> None:
        if self.root is None and not self.invalid_root:
            return
        try:
            if self.invalid_root or self.root is None:
                raise OSError("invalid journal root")
            _append(self.root, phase, self.family, self.invocation_id, result)
        except Exception:
            if not self.warned:
                try:
                    print(_WARNING, file=sys.stderr)
                except Exception:
                    pass
                self.warned = True

    def finish(self, result: str) -> None:
        self._record("end", result)


def begin(family: object) -> CommandJournal:
    safe = family if isinstance(family, str) and family in _FAMILIES else "unknown"
    root, invalid = _root()
    try:
        invocation_id = uuid.uuid4().hex
    except Exception:
        invocation_id = ""
        invalid = True
    journal = CommandJournal(safe, root, invalid_root=invalid, invocation_id=invocation_id)
    journal._record("start")
    return journal
