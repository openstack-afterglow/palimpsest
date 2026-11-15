"""Deterministic build-context tar for a checkout that `git_source.fetch` already verified.

The same tree always produces the same bytes and sha256: entries are sorted, timestamps and
ownership are zeroed, and modes are reduced to 0644 (file) / 0755 (directory or executable).
Symlinks are stored as links, never followed, and a link whose target leaves the context is
refused. Special files (FIFOs, devices, sockets) are refused.

Ignore rules come from `.dockerignore` lines. Supported: exact paths, `*`/`?` globs (matched
with `fnmatch`, where `*` also crosses `/`, so it can ignore more than Docker would), and
directory prefixes (ignoring `tmp` drops everything under `tmp/`). Rules that Docker interprets
differently are refused rather than guessed and fail closed: negated rules (`!pattern`), `**`
(Docker's `**/` matches zero or more directories, so `**/.env` also covers a root `.env`, which
`fnmatch` cannot express), and any rule that climbs with `..`.

The rules come from the repository, so their cost is bounded: at most `MAX_IGNORE_RULES` rules of
at most `MAX_NAME_BYTES` bytes, each compiled once and tried once per entry (an ignored directory
is not entered, so an entry's parents are already known not to match), and the matching work for
one context, counted as path characters times rule bytes, may not exceed `MAX_IGNORE_WORK`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tarfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import BinaryIO

MAX_NAME_BYTES = 4096
MAX_IGNORE_RULES = 1000
# Trying one rule costs a fixed call plus about its length times the path length. The work for
# one context is counted as, per entry, (path characters x total rule bytes) plus
# RULE_CALL_WORK per rule, and is bounded. The count does not depend on the host, so the same
# repository gets the same answer everywhere, and it keeps the matching to seconds.
MAX_IGNORE_WORK = 20_000_000_000
RULE_CALL_WORK = 64


class ContextError(ValueError):
    """The checkout cannot be turned into a build context; the reason is safe to report."""


class ContextTooLarge(ContextError):
    """The context has more entries than the caller allows."""


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
        if "**" in line:
            raise ContextError("'**' ignore rules are not supported; they could leave secrets in the context")
        line = line.removeprefix("./").strip("/")
        if not line or ".." in line.split("/"):
            raise ContextError("ignore rule must stay inside the context")
        if len(line.encode()) > MAX_NAME_BYTES:
            raise ContextError("ignore rule is longer than a path can be")
        if "[" in line and any(ord(char) > 0xFF for char in line):
            # Compiling a bracket expression that reaches beyond Latin-1 walks every code point
            # of its ranges; 64 KiB of such rules took 17 seconds before any path was matched.
            raise ContextError("ignore rule with a bracket expression must stay within Latin-1")
        if len(patterns) >= MAX_IGNORE_RULES:
            raise ContextError("too many ignore rules")
        patterns.append(line)
    return tuple(patterns)


class _Rules:
    """Compiled ignore rules with the work they have cost so far."""

    def __init__(self, patterns: Sequence[str]) -> None:
        # The same translation `fnmatch.fnmatchcase` uses, compiled once per rule instead of
        # looked up in its shared cache for every entry.
        self._matchers = tuple(re.compile(fnmatch.translate(pattern)).match for pattern in patterns)
        self._rule_bytes = sum(len(pattern.encode()) for pattern in patterns)
        self._work = 0

    def ignored(self, rel: str) -> bool:
        self._work += len(rel) * self._rule_bytes + RULE_CALL_WORK * len(self._matchers)
        if self._work > MAX_IGNORE_WORK:
            raise ContextError("ignore rules need too much work for this context")
        return any(match(rel) for match in self._matchers)


def _walk(root: Path, rules: _Rules, rel_dir: str = "") -> Iterator[tuple[str, os.DirEntry]]:
    base = root / rel_dir if rel_dir else root
    with os.scandir(base) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    for entry in entries:
        rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            raise ContextError("file name is not valid UTF-8") from None
        if rules.ignored(rel):
            continue
        # The worker's extract_context refuses these; refuse at staging so the job fails here.
        if "\\" in rel or len(rel.encode()) > MAX_NAME_BYTES:
            raise ContextError("file name cannot be extracted by the build worker")
        yield rel, entry
        if entry.is_dir(follow_symlinks=False):
            yield from _walk(root, rules, rel)


def _info(name: str, kind: bytes, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write_context_tar(
    root: Path, out: BinaryIO, *, ignore_lines: Sequence[str] = (), max_entries: int | None = None
) -> tuple[str, int]:
    """Write the context to `out` and return (sha256 hex of the emitted bytes, entry count).

    With `max_entries`, packing stops with `ContextTooLarge` at the first entry beyond it.
    """
    real_root = Path(root).resolve(strict=True)
    if not real_root.is_dir():
        raise ContextError("context root is not a directory")
    rules = _Rules(parse_ignore(ignore_lines))
    hashing = _Hashing(out)
    count = 0
    with tarfile.open(fileobj=hashing, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for rel, entry in _walk(real_root, rules):
            if max_entries is not None and count >= max_entries:
                raise ContextTooLarge("build context has too many entries")
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
