"""Deterministic build-context tar for a checkout that `git_source.fetch` already verified.

The same tree always produces the same bytes and sha256: entries are sorted, timestamps and
ownership are zeroed, and modes are reduced to 0644 (file) / 0755 (directory or executable).
Symlinks are stored as links, never followed, and a link whose target leaves the context is
refused. Special files (FIFOs, devices, sockets) are refused.

Ignore rules come from `.dockerignore` lines. Supported: exact paths, `*`/`?` globs (matched
with `fnmatch`, so `*` also crosses `/`: a broad pattern ignores more, which is the safe
direction), and directory prefixes (ignoring `tmp` drops everything under `tmp/`). Negated
rules (`!pattern`) are refused rather than guessed, and a pattern that climbs with `..` is
refused.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import tarfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import BinaryIO


class ContextError(ValueError):
    """The checkout cannot be turned into a build context; the reason is safe to report."""


class _Hashing:
    """Write-through wrapper so the archive digest is computed over the exact emitted bytes."""

    def __init__(self, out: BinaryIO) -> None:
        self._out = out
        self.sha256 = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.sha256.update(data)
        return self._out.write(data)


def parse_ignore(lines: Sequence[str]) -> tuple[str, ...]:
    patterns: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            raise ContextError("negated ignore rules are not supported")
        line = line.removeprefix("./").strip("/")
        if not line or ".." in line.split("/"):
            raise ContextError("ignore rule must stay inside the context")
        patterns.append(line)
    return tuple(patterns)


def _ignored(rel: str, patterns: Sequence[str]) -> bool:
    parts = rel.split("/")
    prefixes = ("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return any(fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes for pattern in patterns)


def _walk(root: Path, patterns: Sequence[str], rel_dir: str = "") -> Iterator[tuple[str, os.DirEntry]]:
    base = root / rel_dir if rel_dir else root
    with os.scandir(base) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    for entry in entries:
        rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            raise ContextError("file name is not valid UTF-8") from None
        if _ignored(rel, patterns):
            continue
        yield rel, entry
        if entry.is_dir(follow_symlinks=False):
            yield from _walk(root, patterns, rel)


def _info(name: str, kind: bytes, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write_context_tar(root: Path, out: BinaryIO, *, ignore_lines: Sequence[str] = ()) -> tuple[str, int]:
    """Write the context to `out` and return (sha256 hex of the emitted bytes, entry count)."""
    real_root = Path(root).resolve(strict=True)
    if not real_root.is_dir():
        raise ContextError("context root is not a directory")
    patterns = parse_ignore(ignore_lines)
    hashing = _Hashing(out)
    count = 0
    with tarfile.open(fileobj=hashing, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for rel, entry in _walk(real_root, patterns):
            path = Path(entry.path)
            if entry.is_symlink():
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise ContextError("symlink target must be relative")
                resolved = (path.parent / target).resolve(strict=False)
                if not resolved.is_relative_to(real_root):
                    raise ContextError("symlink escapes the context")
                info = _info(rel, tarfile.SYMTYPE, 0o777)
                info.linkname = target
                tar.addfile(info)
            elif entry.is_dir(follow_symlinks=False):
                tar.addfile(_info(rel, tarfile.DIRTYPE, 0o755))
            elif entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                mode = 0o755 if st.st_mode & 0o111 else 0o644
                info = _info(rel, tarfile.REGTYPE, mode)
                info.size = st.st_size
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                raise ContextError("special files are not allowed in the context")
            count += 1
    return hashing.sha256.hexdigest(), count
