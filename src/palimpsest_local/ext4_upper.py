"""Read an overlay upper directory out of a private ext4 image copy with e2fsprogs ``debugfs``.

The image must be a disposable clone of a stopped instance's upper disk: this module only
runs ``debugfs -c`` (catastrophic, read-only) and never mounts or writes. Whiteouts are
character devices 0/0, opaque directories carry the overlay opaque xattr, and every entry is
reported with its path relative to the guest root so ``oci_capture`` can convert it.

Every unexpected output shape fails closed with a typed ``CaptureError``; nothing is skipped.
"""

from __future__ import annotations

import re
import resource
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .oci_capture import CaptureError, UpperEntry, UpperKind

# VZ guest layout (guest/vz/platform.c): the upper lives at /upper of the data ext4; /work and
# /platform-state are siblings and are never part of the captured upper.
DEFAULT_UPPER = "upper"
BINDING_PATH = "/platform-state/binding.json"
BINDING_SCHEMA = "palimpsest.vz-upper-binding.v1"
MAX_ENTRIES = 1_000_000
_LS = re.compile(r"^/(\d+)/(\d+)/(\d+)/(\d+)/(.*)/(\d*)/$")
_EA_NAME = re.compile(r"^  (\S+) \((\d+)\)")
_EA_HEX = re.compile(r"^(\S+) \((\d+)\) = ((?:[0-9a-f]{2} )*)$")
_MTIME = re.compile(r"^ mtime: 0x([0-9a-f]+)(?::[0-9a-f]+)? --")
_LINKS = re.compile(r"^Links: (\d+)")
_SIZE = re.compile(r"^User:\s+\d+\s+Group:\s+\d+\s+Project:\s+\d+\s+Size: (\d+)$")
_BLOCKS = re.compile(r"^Links: \d+\s+Blockcount: (\d+)$")
_FLAGS = re.compile(r"^Inode: \d+\s+Type: .*Flags: 0x([0-9a-f]+)$")
_DUMP = re.compile(r"^([0-7]{4})  ((?:[0-9a-f]{4}(?: |$)){1,8})")
# Everything printed from here on can contain guest-controlled text (xattr values, link targets).
_UNTRUSTED_FROM = ("Size of extra inode fields:", "Extended attributes:", "Inode checksum:", "EXTENTS:", "BLOCKS:")
_EXTENTS_FL = 0x80000
_INLINE_DATA_FL = 0x10000000
_FAST_LINK_MAX = 59
_PROMPT = "debugfs: "
_ERROR = re.compile(r"^(?:ls|stat|ea_list|ea_get|cat)(?: -\w+)*: ")


class JournalNotClean(CaptureError):
    """The image still needs journal recovery, so reading it would miss the latest changes."""

    def __init__(self) -> None:
        super().__init__("ext4-journal-dirty", "the filesystem journal needs recovery; refusing to read a stale view")


def _limit() -> None:
    """Child limits for parsing a guest-controlled filesystem: bounded CPU, no file creation, no core."""
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def limited_run(*args, **kwargs):
    return subprocess.run(*args, preexec_fn=_limit, **kwargs)


def limited_popen(*args, **kwargs):
    return subprocess.Popen(*args, preexec_fn=_limit, **kwargs)


@dataclass(frozen=True, slots=True)
class Ext4Payload:
    image: Path
    inode: int
    size: int


@dataclass(frozen=True, slots=True)
class StagedPayload:
    """A file already dumped into the private scratch directory."""

    path: Path
    size: int


MAX_STAGED_FILE = 8 * 1024**3


def _limit_writer() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_STAGED_FILE, MAX_STAGED_FILE))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def limited_writer_run(*args, **kwargs):
    """Like ``limited_run`` but allows writing the staged files into the private scratch."""
    return subprocess.run(*args, preexec_fn=_limit_writer, **kwargs)


def _fail(detail: str) -> CaptureError:
    return CaptureError("ext4-upper", detail)


def _run(debugfs: Path, image: Path, commands: list[str], runner) -> dict[str, list[str]]:
    """Run commands in one debugfs session; return the output lines of each command."""
    script = "\n".join(commands) + "\n"
    try:
        done = runner(
            [str(debugfs), "-c", "-f", "/dev/stdin", str(image)],
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail(f"debugfs could not run: {type(exc).__name__}") from None
    if done.returncode != 0:
        raise _fail("debugfs exited with an error")
    # Guest-controlled names, link targets and xattr values reach stdout raw, so the prompt
    # delimiter is only trusted positionally: every prompt line must be exactly the next
    # expected command and there must be no other line that looks like a prompt. An injected
    # prompt-looking line therefore changes the count or the order and is refused.
    # latin-1 decodes any byte losslessly (the inode dump gutter holds raw bytes); names are
    # re-decoded as strict UTF-8 where they are used.
    lines = done.stdout.decode("latin-1").split("\n")
    prompts = [index for index, line in enumerate(lines) if line.startswith(_PROMPT)]
    if len(prompts) != len(commands):
        raise _fail("debugfs output contained an unexpected prompt line")
    results: dict[str, list[str]] = {}
    for position, command in enumerate(commands):
        if lines[prompts[position]] != _PROMPT + command or command in results:
            raise _fail("debugfs output is out of order or ambiguous")
        stop = prompts[position + 1] if position + 1 < len(prompts) else len(lines)
        results[command] = lines[prompts[position] + 1 : stop]
    for command, body in results.items():
        for line in body:
            if not line.startswith("/") and (_ERROR.match(line) or "ext2_lookup" in line):
                raise _fail(f"debugfs reported an error for {command!r}")
    return results


def _parse_dump(lines: list[str]) -> bytes:
    """Decode `inode_dump -b` hex (i_block, 60 bytes); `*` repeats the previous line."""
    data = bytearray()
    previous = b""
    pending_repeat = False
    for line in lines:
        if not line:
            continue
        if line == "*":
            pending_repeat = True
            continue
        match = _DUMP.match(line)
        if match is None:
            raise _fail("unparseable inode dump line")
        offset = int(match.group(1), 8)
        if pending_repeat:
            while len(data) < offset:
                data += previous
            pending_repeat = False
        if offset != len(data):
            raise _fail("inode dump offsets are inconsistent")
        chunk = bytes.fromhex(match.group(2).replace(" ", ""))
        data += chunk
        previous = chunk
    if len(data) < 60:
        data += bytes(60 - len(data))
    return bytes(data[:60])


def _kind(mode: int) -> UpperKind:
    fmt = stat.S_IFMT(mode)
    if fmt == stat.S_IFREG:
        return UpperKind.FILE
    if fmt == stat.S_IFDIR:
        return UpperKind.DIRECTORY
    if fmt == stat.S_IFLNK:
        return UpperKind.SYMLINK
    return UpperKind.OTHER


def _name(raw: str) -> str:
    try:
        name = raw.encode("latin-1").decode("utf-8")
    except UnicodeError:
        raise _fail("entry name is not valid UTF-8") from None
    if not name or "/" in name or name in (".", "..") or "\x00" in name or "\n" in name:
        raise _fail("entry name is not representable")
    return name


def is_journal_dirty(image: Path, debugfs: Path, *, runner=limited_run) -> bool:
    """True when the superblock still carries ``needs_recovery``."""
    lines = _run(Path(debugfs), Path(image), ["features"], runner)["features"]
    features = next((line for line in lines if line.startswith("Filesystem features:")), None)
    if features is None:
        raise _fail("could not read the filesystem features")
    return "needs_recovery" in features.split()[2:]


def read_binding(image: Path, debugfs: Path, *, runner=limited_run) -> dict:
    """Read and parse the guest's upper binding record from the data ext4."""
    import json

    try:
        done = runner(
            [str(debugfs), "-c", "-R", f"cat {BINDING_PATH}", str(image)], capture_output=True, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail(f"debugfs could not run: {type(exc).__name__}") from None
    if done.returncode != 0 or not done.stdout or len(done.stdout) > 8192:
        raise _fail("the upper binding record is missing or oversized")
    try:
        value = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _fail("the upper binding record is not valid JSON") from None
    if not isinstance(value, dict) or value.get("schema") != BINDING_SCHEMA:
        raise _fail("the upper binding record has an unexpected schema")
    return value


def read_upper(
    image: Path,
    debugfs: Path,
    *,
    upper: str = DEFAULT_UPPER,
    runner=limited_run,
    max_entries: int = MAX_ENTRIES,
) -> list[UpperEntry]:
    """Return every upper entry of the image, in canonical path order, with payload handles.

    Refuses an image whose journal still needs recovery (``debugfs -c`` never replays it).
    """
    image = Path(image)
    debugfs = Path(debugfs)
    if is_journal_dirty(image, debugfs, runner=runner):
        raise JournalNotClean()
    root_listing = _run(debugfs, image, [f"stat /{upper}"], runner)[f"stat /{upper}"]
    match = next((re.match(r"^Inode: (\d+)\s+Type: directory", line) for line in root_listing if line), None)
    if match is None:
        raise _fail("the upper directory is missing or not a directory")
    top = int(match.group(1))

    # Breadth-first listing, one debugfs session per directory level.
    dirs = [(top, "")]
    records: dict[str, tuple[int, int, int, int, int]] = {}
    while dirs:
        commands = [f"ls -p <{ino}>" for ino, _ in dirs]
        outputs = _run(debugfs, image, commands, runner)
        nxt: list[tuple[int, str]] = []
        for ino, prefix in dirs:
            for line in outputs[f"ls -p <{ino}>"]:
                if not line or line.startswith("debugfs "):
                    continue
                m = _LS.match(line)
                if m is None:
                    raise _fail("unparseable directory listing line")
                child, mode, uid, gid, raw, size = m.groups()
                if raw in (".", ".."):
                    continue
                path = f"{prefix}{_name(raw)}"
                mode_i = int(mode, 8)
                records[path] = (int(child), mode_i, int(uid), int(gid), int(size or 0))
                if len(records) > max_entries:
                    raise _fail("upper exceeds the entry limit")
                if stat.S_ISDIR(mode_i):
                    nxt.append((int(child), path + "/"))
        dirs = nxt

    inodes = sorted({rec[0] for rec in records.values()})
    dump_inodes = {rec[0] for rec in records.values() if stat.S_ISLNK(rec[1]) or stat.S_ISCHR(rec[1])}
    details: dict[int, dict] = {}
    for start in range(0, len(inodes), 512):
        chunk = inodes[start : start + 512]
        dumps = [ino for ino in chunk if ino in dump_inodes]
        commands = (
            [f"stat <{ino}>" for ino in chunk]
            + [f"ea_list <{ino}>" for ino in chunk]
            + [f"inode_dump -b <{ino}>" for ino in dumps]
        )
        outputs = _run(debugfs, image, commands, runner)
        for ino in chunk:
            info: dict = {"mtime": 0, "links": 1, "size": 0, "blocks": 0, "flags": 0, "iblock": None, "ea": []}
            seen: set[str] = set()
            for line in outputs[f"stat <{ino}>"]:
                if line.startswith(_UNTRUSTED_FROM):
                    break
                for name, pattern, convert in (
                    ("mtime", _MTIME, lambda m: int(m.group(1), 16)),
                    ("links", _LINKS, lambda m: int(m.group(1))),
                    ("size", _SIZE, lambda m: int(m.group(1))),
                    ("blocks", _BLOCKS, lambda m: int(m.group(1))),
                    ("flags", _FLAGS, lambda m: int(m.group(1), 16)),
                ):
                    if name not in seen and (m := pattern.match(line)) is not None:
                        seen.add(name)
                        info[name] = convert(m)
            names = [_name(m.group(1)) for line in outputs[f"ea_list <{ino}>"] if (m := _EA_NAME.match(line))]
            info["ea"] = names
            if ino in dumps:
                info["iblock"] = _parse_dump(outputs[f"inode_dump -b <{ino}>"])
            details[ino] = info
    values: dict[tuple[int, str], bytes] = {}
    wanted = [(ino, name) for ino in inodes for name in details[ino]["ea"]]
    for start in range(0, len(wanted), 512):
        chunk = wanted[start : start + 512]
        commands = [f"ea_get -x <{ino}> {name}" for ino, name in chunk]
        outputs = _run(debugfs, image, commands, runner)
        for ino, name in chunk:
            lines = [line for line in outputs[f"ea_get -x <{ino}> {name}"] if line]
            m = _EA_HEX.match(lines[0]) if lines else None
            if m is None or m.group(1) != name:
                raise _fail("unparseable extended attribute value")
            data = bytes.fromhex(m.group(3).replace(" ", ""))
            if len(data) != int(m.group(2)):
                raise _fail("extended attribute length mismatch")
            values[(ino, name)] = data

    def fast_target(ino: int) -> str | None:
        info = details[ino]
        if info["flags"] & _INLINE_DATA_FL:
            raise _fail("inline-data symbolic links are not supported")
        if info["blocks"] == 0 and info["size"] <= _FAST_LINK_MAX and not info["flags"] & _EXTENTS_FL:
            raw = info["iblock"][: info["size"]]
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                raise _fail("symbolic link target is not valid UTF-8") from None
        return None

    slow = sorted({rec[0] for rec in records.values() if stat.S_ISLNK(rec[1]) and fast_target(rec[0]) is None})
    targets: dict[int, str] = {}
    for ino in slow:
        done = runner(
            [str(debugfs), "-c", "-R", f"cat <{ino}>", str(image)], capture_output=True, timeout=120, check=False
        )
        if done.returncode != 0 or not done.stdout:
            raise _fail("could not read a symbolic link target")
        try:
            targets[ino] = done.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise _fail("symbolic link target is not valid UTF-8") from None

    entries: list[UpperEntry] = []
    first_path: dict[int, str] = {}
    for path in sorted(records):
        ino, mode, uid, gid, size = records[path]
        info = details[ino]
        kind = _kind(mode)
        xattrs = tuple((name, values[(ino, name)]) for name in sorted(info["ea"]))
        link_target = ""
        payload = None
        if kind is UpperKind.OTHER and stat.S_ISCHR(mode) and info["iblock"][:8] == bytes(8):
            kind = UpperKind.WHITEOUT
        elif kind is UpperKind.SYMLINK:
            fast = fast_target(ino)
            link_target = fast if fast is not None else targets[ino]
        elif kind is UpperKind.FILE:
            if info["links"] > 1 and ino in first_path:
                kind = UpperKind.HARDLINK
                link_target = first_path[ino]
                size = 0
            else:
                first_path[ino] = path
                payload = Ext4Payload(image, ino, size)
        elif kind is UpperKind.DIRECTORY:
            size = 0
        entries.append(
            UpperEntry(
                path=path,
                kind=kind,
                mode=stat.S_IMODE(mode),
                uid=uid,
                gid=gid,
                mtime=info["mtime"],
                size=size,
                link_target=link_target,
                xattrs=xattrs,
                payload=payload,
            )
        )
    return entries


def stage_payloads(
    entries: list[UpperEntry],
    debugfs: Path,
    scratch: Path,
    *,
    batch: int = 1000,
    runner=limited_writer_run,
) -> list[UpperEntry]:
    """Dump every regular-file payload with few debugfs sessions instead of one process per file.

    Returns new entries whose payload is a ``StagedPayload`` in ``scratch`` (a private directory
    the caller removes). Every dumped file must match the size recorded for its inode.
    """
    from dataclasses import replace

    scratch = Path(scratch)
    if any(ch.isspace() or ch in "\"'\\" for ch in str(scratch)):
        raise _fail("the scratch path must not contain whitespace or quotes")
    wanted = {}
    for entry in entries:
        if isinstance(entry.payload, Ext4Payload):
            wanted[entry.payload.inode] = entry.payload
    if not wanted:
        return list(entries)
    image = next(iter(wanted.values())).image
    inodes = sorted(wanted)
    for start in range(0, len(inodes), batch):
        chunk = inodes[start : start + batch]
        commands = [f"dump <{ino}> {scratch / str(ino)}" for ino in chunk]
        _run(Path(debugfs), image, commands, runner)
    staged = {}
    for ino in inodes:
        path = scratch / str(ino)
        try:
            info = path.lstat()
        except OSError:
            raise _fail("a file payload was not dumped") from None
        if not stat.S_ISREG(info.st_mode) or info.st_size != wanted[ino].size:
            raise _fail("a dumped file does not match its recorded size")
        staged[ino] = StagedPayload(path, info.st_size)
    return [
        replace(entry, payload=staged[entry.payload.inode]) if isinstance(entry.payload, Ext4Payload) else entry
        for entry in entries
    ]


def make_payload_opener(debugfs: Path, *, popen=limited_popen):
    """Return the ``open_payload`` callable ``convert_upper_to_layer`` needs for ``Ext4Payload`` handles."""

    @contextmanager
    def open_payload(payload: object) -> Iterator[BinaryIO]:
        if isinstance(payload, StagedPayload):
            with payload.path.open("rb") as handle:
                yield handle
            return
        if not isinstance(payload, Ext4Payload):
            raise _fail("unexpected payload handle")
        process = popen(
            [str(debugfs), "-c", "-R", f"cat <{payload.inode}>", str(payload.image)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield process.stdout
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.wait() != 0:
                raise _fail("debugfs failed while streaming a file")

    return open_payload


__all__ = [
    "BINDING_SCHEMA",
    "DEFAULT_UPPER",
    "Ext4Payload",
    "StagedPayload",
    "JournalNotClean",
    "is_journal_dirty",
    "limited_run",
    "make_payload_opener",
    "read_binding",
    "read_upper",
    "stage_payloads",
]
