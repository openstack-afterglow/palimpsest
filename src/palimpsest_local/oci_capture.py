"""Convert a captured overlay upper into one deterministic OCI layer.

Pure transformation: the caller supplies a stream of upper entries (never the live
upper directory), the mandatory-exclusion inputs and an optional ``.plmignore``.
Nothing here opens, modifies or deletes the source upper.
"""

from __future__ import annotations

import fnmatch
import hashlib
import posixpath
import re
import tarfile
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import BinaryIO, Protocol

from .errors import ArtifactValidationError

OCI_CAPTURE_ID = "palimpsest.oci-capture.v1"
_MAX_PATH_BYTES = 4096
_WHITEOUT_PREFIX = ".wh."
_OPAQUE_MARKER = ".wh..wh..opq"
_OVERLAY_OPAQUE_NAMES = ("trusted.overlay.opaque", "user.overlay.opaque")
_OVERLAY_PREFIXES = ("trusted.overlay.", "user.overlay.")
# Kernel copy-up bookkeeping with no meaning in the merged view (``impure`` marks a directory
# holding copied-up children, ``origin`` is the file handle of the lower file). They are observed
# on every real overlay upper and are dropped, never written to the layer.
_OVERLAY_BOOKKEEPING_NAMES = (
    "trusted.overlay.impure",
    "user.overlay.impure",
    "trusted.overlay.origin",
    "user.overlay.origin",
)

# Always excluded subtree (platform/control/access namespace).
_RESERVED_ROOT = ".palimpsest"
# Cloud-init instance state root directory.
_CLOUD_STATE_ROOT = "var/lib/cloud"
# Package database: always captured so installed packages stay consistent; .plmignore cannot drop it.
_PACKAGE_DB_ROOT = "var/lib/dpkg"
# Machine identity state hidden from every new VM when it exists in a parent layer.
_IDENTITY_GLOBS = (
    "etc/ssh/ssh_host_*",
    "var/lib/dhcp/*.leases",
    "var/lib/dhclient/*.leases",
    "var/lib/dhcp/*",
    "var/lib/dhclient/*",
    "var/lib/NetworkManager/*.lease*",
)
# Platform-bound identity files: excluded only as recorded generated placeholders.
_BOUND_IDENTITY_PATHS = ("etc/machine-id", "var/lib/dbus/machine-id", "etc/hostname", "etc/resolv.conf")
# Provenance markers for explicit platform injection proof.
_INJECTED_PROVENANCES = frozenset({"platform-injected", "injected", "placeholder", "generated"})
_USER_PROVENANCES = frozenset({"", "user", "user-authored"})
# Bound on .plmignore content size matching project ignore contract (1 MiB).
_MAX_PLMIGNORE_BYTES = 1024 * 1024


class CaptureError(ArtifactValidationError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


class UpperKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    WHITEOUT = "whiteout"  # overlay character device 0/0
    OTHER = "other"  # fifo, socket, block or non-whiteout character device


@dataclass(frozen=True, slots=True)
class UpperEntry:
    """One entry of a consistent upper copy, path relative to the guest root."""

    path: str
    kind: UpperKind
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    mtime: int = 0
    size: int = 0
    link_target: str = ""
    xattrs: tuple[tuple[str, bytes], ...] = ()
    payload: object | None = None
    provenance: str = ""


class _Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class CaptureReceipt:
    diff_id: str
    size: int
    entries: int
    excluded: tuple[str, ...] = field(default=())
    ignored: tuple[str, ...] = field(default=())
    hidden: tuple[str, ...] = field(default=())
    # Paths a .plmignore rule matched but which the capture policy always includes.
    retained: tuple[str, ...] = field(default=())


def normalize_capture_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\0" in path:
        raise CaptureError("capture-path", "entry path is invalid")
    if len(path.encode("utf-8", "surrogateescape")) > _MAX_PATH_BYTES:
        raise CaptureError("capture-path", "entry path exceeds its bound")
    if path.startswith("/"):
        raise CaptureError("capture-path", "entry path must be relative")
    clean = posixpath.normpath(path)
    if clean in {".", ""} or clean == ".." or clean.startswith("../") or clean != path.rstrip("/"):
        raise CaptureError("capture-path", "entry path is not normalized or escapes the root")
    return clean


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _prefixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _is_plausible_authorized_keys_path(path: str) -> bool:
    """Conservatively identify any plausible authorized_keys path regardless of home root."""
    return posixpath.basename(path) in {"authorized_keys", "authorized_keys2"}


def _is_mandatory(
    path: str,
    placeholders: Collection[str],
    *,
    injected_keys: Collection[str] = (),
    user_keys: Collection[str] = (),
    entry: UpperEntry | None = None,
) -> bool:
    if _under(path, _RESERVED_ROOT):
        return True
    if path in _BOUND_IDENTITY_PATHS:
        return path in placeholders or (entry is not None and entry.provenance in _INJECTED_PROVENANCES)
    if _under(path, _CLOUD_STATE_ROOT):
        return True
    if _is_plausible_authorized_keys_path(path):
        if path in injected_keys:
            return True
        if path in user_keys:
            return False
        raise CaptureError(
            "capture-provenance",
            f"unclassified authorized_keys path requires explicit trusted classification: {path}",
        )
    for pattern in _IDENTITY_GLOBS:
        if "*" in pattern:
            if fnmatch.fnmatchcase(path, pattern) or any(
                fnmatch.fnmatchcase(prefix, pattern) for prefix in _prefixes(path)
            ):
                return True
        else:
            if _under(path, pattern):
                return True
    return False


# Ignore-rule bounds. A /.plmignore comes from the guest or from an image, so matching must not
# be able to run without limit: patterns with more than two unbounded wildcards are matched by a
# linear state simulation instead of the backtracking regex engine, and the whole evaluation has
# a rule cap and a time budget that end in a refusal, never in a silent pass.
_MAX_PLMIGNORE_RULES = 10_000
_MAX_REGEX_WILDCARDS = 2
_PLMIGNORE_BUDGET_SECONDS = 120.0
_UNBOUNDED = ("star", "globstar", "globdir")


def _glob_tokens(pattern: str) -> list[tuple[str, str]]:
    """Tokens ``(kind, text)``: ``star`` is ``*`` (no "/"), ``qmark`` is ``?`` (no "/"),
    ``globstar`` is ``**`` (anything but a newline), ``globdir`` is ``**/`` (zero or more whole
    directories), ``class`` is a bracket expression as regex text and ``lit`` one literal character."""
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                index += 2
                if pattern[index : index + 1] == "/":
                    index += 1
                    tokens.append(("globdir", ""))
                else:
                    tokens.append(("globstar", ""))
                continue
            tokens.append(("star", ""))
        elif char == "?":
            tokens.append(("qmark", ""))
        elif char == "[":
            end = pattern.find("]", index + 2)
            if end == -1:
                tokens.append(("lit", char))
            else:
                body = pattern[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                tokens.append(("class", "[" + body.replace("\\", "\\\\") + "]"))
                index = end
        elif char == "\\" and index + 1 < len(pattern):
            index += 1
            tokens.append(("lit", pattern[index]))
        else:
            tokens.append(("lit", char))
        index += 1
    return tokens


def _regex(text: str, problem: str = ".plmignore has an invalid character class") -> re.Pattern[str]:
    try:
        return re.compile(text)
    except re.error:
        raise CaptureError("plmignore", problem) from None


def _tokens_to_regex(tokens: list[tuple[str, str]]) -> re.Pattern[str]:
    parts = {"star": "[^/]*", "qmark": "[^/]", "globstar": ".*", "globdir": "(?:.*/)?"}
    out = [text if kind == "class" else re.escape(text) if kind == "lit" else parts[kind] for kind, text in tokens]
    return _regex("^" + "".join(out) + "$")


class _LinearGlob:
    """Match one pattern by simulating its states; time is pattern length times path length.

    It accepts exactly what the regex translation accepts, including that ``**`` does not cross a
    newline and that one trailing newline in the path is ignored at the end of the pattern. A
    bracket expression that is a class only together with the text after it (``[!]...]``) has no
    token of its own; such a pattern is refused here rather than matched approximately.
    """

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self._kinds = [kind for kind, _ in tokens]
        self._literals = [text for _, text in tokens]
        ambiguous = ".plmignore combines an ambiguous character class with more than two wildcards"
        self._classes = [_regex(text, ambiguous).match if kind == "class" else None for kind, text in tokens]
        self._size = len(tokens)

    def _closure(self, states: set[int]) -> set[int]:
        # State i: before token i. State size + 1 + i: inside the ``**/`` group of token i.
        size, kinds = self._size, self._kinds
        pending = list(states)
        while pending:
            state = pending.pop()
            if state >= size or kinds[state] not in _UNBOUNDED:
                continue
            for following in (state + 1, size + 1 + state) if kinds[state] == "globdir" else (state + 1,):
                if following not in states:
                    states.add(following)
                    pending.append(following)
        return states

    def match(self, text: str) -> bool:
        size, kinds, literals, classes = self._size, self._kinds, self._literals, self._classes
        states = self._closure({0})
        last = len(text) - 1
        for position, char in enumerate(text):
            if position == last and char == "\n" and size in states:
                return True
            moved: set[int] = set()
            for state in states:
                if state == size:
                    continue
                if state > size:
                    if char != "\n":
                        moved.add(state)
                        if char == "/":
                            moved.add(state - size)
                    continue
                kind = kinds[state]
                if kind == "lit":
                    if char == literals[state]:
                        moved.add(state + 1)
                elif kind == "star":
                    if char != "/":
                        moved.add(state)
                elif kind == "qmark":
                    if char != "/":
                        moved.add(state + 1)
                elif kind == "globstar":
                    if char != "\n":
                        moved.add(state)
                elif kind == "class" and classes[state](char):
                    moved.add(state + 1)
            if not moved:
                return False
            states = self._closure(moved)
        return size in states


def _compile_rule(pattern: str) -> Callable[[str], object]:
    tokens = _glob_tokens(pattern)
    regex = _tokens_to_regex(tokens)  # also validates character classes
    if sum(1 for kind, _ in tokens if kind in _UNBOUNDED) <= _MAX_REGEX_WILDCARDS:
        return regex.match
    return _LinearGlob(tokens).match


class IgnoreMatcher:
    """Docker-ignore syntax: last matching rule wins; ``!`` re-includes; a rule that
    matches a directory also covers its descendants."""

    def __init__(self, text: str | bytes | None, *, budget_seconds: float = _PLMIGNORE_BUDGET_SECONDS) -> None:
        self._rules: list[tuple[bool, Callable[[str], object]]] = []
        self._budget = budget_seconds
        self._spent = 0.0
        if text is None:
            return
        if isinstance(text, bytes):
            if len(text) > _MAX_PLMIGNORE_BYTES:
                raise CaptureError("plmignore", ".plmignore exceeds its byte limit")
            try:
                text = text.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
            except UnicodeDecodeError:
                raise CaptureError("plmignore", ".plmignore is not UTF-8") from None
        elif isinstance(text, str):
            if len(text.encode("utf-8", "surrogateescape")) > _MAX_PLMIGNORE_BYTES:
                raise CaptureError("plmignore", ".plmignore exceeds its byte limit")
        else:
            raise CaptureError("plmignore", "invalid .plmignore content type")
        if "\0" in text:
            raise CaptureError("plmignore", ".plmignore contains NUL")
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:].strip()
            line = posixpath.normpath(line.strip("/")) if line.strip("/") else ""
            if not line or line == ".":
                continue
            if len(self._rules) >= _MAX_PLMIGNORE_RULES:
                raise CaptureError("plmignore", ".plmignore has too many rules")
            self._rules.append((negate, _compile_rule(line)))

    def ignored(self, path: str, *, is_dir: bool = False) -> bool:
        if not self._rules:
            return False
        started = time.monotonic()
        state = False
        parts = path.split("/")
        prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        for negate, match in self._rules:
            if any(match(candidate) for candidate in prefixes):
                state = not negate
        self._spent += time.monotonic() - started
        if self._spent > self._budget:
            raise CaptureError("plmignore", ".plmignore rules took too long to evaluate")
        return state


class _DigestSink:
    def __init__(self, sink: BinaryIO, maximum: int | None) -> None:
        self._sink = sink
        self._maximum = maximum
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, payload: bytes) -> int:
        if self._maximum is not None and len(payload) > self._maximum - self.size:
            raise CaptureError("capture-limit", "layer output exceeds its byte limit")
        written = self._sink.write(payload)
        if written != len(payload):
            raise CaptureError("capture-write", "layer output write was incomplete")
        self._digest.update(payload)
        self.size += len(payload)
        return written

    def flush(self) -> None:
        self._sink.flush()

    @property
    def digest(self) -> str:
        return "sha256:" + self._digest.hexdigest()


def _pax_xattrs(items: Iterable[tuple[str, bytes]]) -> dict[str, str]:
    res = {}
    for name, value in items:
        if "\0" in name:
            raise CaptureError("capture-xattr", f"xattr name {name!r} contains NUL")
        res[f"SCHILY.xattr.{name}"] = value.decode("utf-8", "surrogateescape")
    return res


def _marker(path: str, name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=posixpath.join(posixpath.dirname(path), name) if posixpath.dirname(path) else name)
    info.type = tarfile.REGTYPE
    info.mode = 0
    info.uid = info.gid = 0
    info.mtime = 0
    info.size = 0
    return info


def _emitted_tar_name(path: str, role: str, entry: UpperEntry | None) -> str:
    if role == "hide" or (entry is not None and entry.kind is UpperKind.WHITEOUT):
        dirname = posixpath.dirname(path)
        wh = _WHITEOUT_PREFIX + posixpath.basename(path)
        return posixpath.join(dirname, wh) if dirname else wh
    return path


def resolve_commit_plmignore(
    ordered_entries: Mapping[str, UpperEntry],
    open_payload: Callable[[object], AbstractContextManager[_Readable]],
    parent_plmignore: str | bytes | CaptureError | None = None,
) -> str | None:
    """Resolve guest root /.plmignore: upper entry wins if present; otherwise parent.

    A parent file the caller found unusable is passed as the ``CaptureError`` to raise; it is
    raised only when the upper has no entry of its own.
    """
    if ".plmignore" in ordered_entries:
        entry = ordered_entries[".plmignore"]
        if entry.kind is UpperKind.FILE and entry.payload is not None:
            if entry.size < 0:
                raise CaptureError("plmignore", "negative .plmignore size is invalid")
            if entry.size > _MAX_PLMIGNORE_BYTES:
                raise CaptureError("plmignore", ".plmignore exceeds its byte limit")
            with open_payload(entry.payload) as stream:
                raw = stream.read(_MAX_PLMIGNORE_BYTES + 1)
                if len(raw) > _MAX_PLMIGNORE_BYTES:
                    raise CaptureError("plmignore", ".plmignore exceeds its byte limit")
                if not isinstance(raw, bytes):
                    return str(raw)
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise CaptureError("plmignore", ".plmignore is not UTF-8") from None
        if entry.kind is UpperKind.WHITEOUT:
            return None
        if entry.kind is not UpperKind.FILE:
            raise CaptureError("plmignore", ".plmignore must be a regular file or whiteout")
    if parent_plmignore is not None:
        if isinstance(parent_plmignore, CaptureError):
            raise parent_plmignore
        if isinstance(parent_plmignore, bytes):
            if len(parent_plmignore) > _MAX_PLMIGNORE_BYTES:
                raise CaptureError("plmignore", "parent .plmignore exceeds its byte limit")
            try:
                return parent_plmignore.decode("utf-8")
            except UnicodeDecodeError:
                raise CaptureError("plmignore", "parent .plmignore is not UTF-8") from None
        if isinstance(parent_plmignore, str):
            if len(parent_plmignore.encode("utf-8", "surrogateescape")) > _MAX_PLMIGNORE_BYTES:
                raise CaptureError("plmignore", "parent .plmignore exceeds its byte limit")
            return parent_plmignore
        raise CaptureError("plmignore", "parent .plmignore is invalid")
    return None


def convert_upper_to_layer(
    entries: Iterable[UpperEntry],
    sink: BinaryIO,
    open_payload: Callable[[object], AbstractContextManager[_Readable]],
    *,
    plmignore: str | bytes | None = None,
    parent_plmignore: str | bytes | CaptureError | None = None,
    generated_placeholders: Collection[str] = (),
    injected_placeholders: Collection[str] = (),
    user_authored_keys: Collection[str] = (),
    authorized_keys_provenance: Mapping[str, str] | None = None,
    parent_paths: Collection[str] = (),
    max_entries: int = 1_000_000,
    max_bytes: int | None = None,
) -> CaptureReceipt:
    """Write one deterministic OCI layer tar for the surviving upper entries.

    Selection order: mandatory exclusion (always wins) -> ``.plmignore`` (re-inclusion
    cannot reach mandatory exclusions) -> include. Identity paths present in a parent
    layer and not already represented are hidden with an explicit whiteout.
    """
    if not callable(open_payload) or not hasattr(sink, "write"):
        raise CaptureError("capture-boundary", "capture boundary is invalid")
    injected_keys = {normalize_capture_path(item) for item in generated_placeholders} | {
        normalize_capture_path(item) for item in injected_placeholders
    }
    user_keys = {normalize_capture_path(item) for item in user_authored_keys}

    if authorized_keys_provenance is not None:
        for raw_path, prov in authorized_keys_provenance.items():
            norm = normalize_capture_path(raw_path)
            if not isinstance(prov, str):
                raise CaptureError("capture-provenance", "authorized_keys_provenance value must be a string")
            if prov in _INJECTED_PROVENANCES:
                injected_keys.add(norm)
            elif prov in _USER_PROVENANCES and prov != "":
                user_keys.add(norm)
            else:
                raise CaptureError("capture-provenance", f"unrecognized provenance for {norm}: {prov!r}")

    conflicts = injected_keys & user_keys
    if conflicts:
        raise CaptureError("capture-provenance", f"conflicting classification for authorized_keys: {conflicts}")

    ordered: dict[str, UpperEntry] = {}
    for entry in entries:
        path = normalize_capture_path(entry.path)
        if path in ordered:
            raise CaptureError("capture-duplicate", "entry path appears twice")
        if len(ordered) >= max_entries:
            raise CaptureError("capture-limit", "entry count exceeds its bound")
        if entry.kind is UpperKind.OTHER:
            raise CaptureError("capture-entry", "special file kinds cannot be captured")
        if not isinstance(entry.provenance, str):
            raise CaptureError("capture-provenance", "entry provenance must be a string")
        if entry.provenance not in _INJECTED_PROVENANCES and entry.provenance not in _USER_PROVENANCES:
            raise CaptureError("capture-provenance", f"unrecognized entry provenance: {entry.provenance!r}")
        if _is_plausible_authorized_keys_path(path):
            if path in injected_keys:
                if entry.provenance in {"user", "user-authored"}:
                    raise CaptureError(
                        "capture-provenance",
                        f"entry provenance conflicts with trusted classification: {path}",
                    )
            elif path in user_keys:
                if entry.provenance in _INJECTED_PROVENANCES:
                    raise CaptureError(
                        "capture-provenance",
                        f"entry provenance conflicts with trusted classification: {path}",
                    )
            else:
                raise CaptureError(
                    "capture-provenance",
                    f"unclassified authorized_keys path requires explicit trusted classification: {path}",
                )
        for name, value in entry.xattrs:
            if name in _OVERLAY_OPAQUE_NAMES:
                if entry.kind is not UpperKind.DIRECTORY:
                    raise CaptureError("capture-overlay-metadata", "overlay opaque on non-directory")
                if value != b"y":
                    raise CaptureError("capture-overlay-metadata", "overlay opaque value is invalid")
            elif name in _OVERLAY_BOOKKEEPING_NAMES:
                continue
            elif any(name.startswith(pfx) for pfx in _OVERLAY_PREFIXES):
                raise CaptureError("capture-overlay-metadata", f"overlay {name} metadata is not supported")
        ordered[path] = entry

    if plmignore is None:
        plmignore = resolve_commit_plmignore(ordered, open_payload, parent_plmignore=parent_plmignore)
    matcher = IgnoreMatcher(plmignore)

    excluded: list[str] = []
    ignored: list[str] = []
    retained: list[str] = []
    kept: dict[str, UpperEntry] = {}
    for path in sorted(ordered):
        entry = ordered[path]
        if _is_mandatory(path, injected_keys, injected_keys=injected_keys, user_keys=user_keys, entry=entry):
            excluded.append(path)
        elif matcher.ignored(path, is_dir=(entry.kind is UpperKind.DIRECTORY)):
            if _under(path, _PACKAGE_DB_ROOT):
                retained.append(path)
                kept[path] = entry
            else:
                ignored.append(path)
        else:
            kept[path] = entry

    hidden: list[str] = []
    for lower in sorted(normalize_capture_path(item) for item in parent_paths):
        if _under(lower, _RESERVED_ROOT) or lower in kept:
            continue
        if lower in _BOUND_IDENTITY_PATHS:
            continue
        if _under(lower, _CLOUD_STATE_ROOT):
            if _CLOUD_STATE_ROOT not in kept and _CLOUD_STATE_ROOT not in hidden:
                hidden.append(_CLOUD_STATE_ROOT)
            continue
        if _is_plausible_authorized_keys_path(lower):
            if lower in injected_keys:
                if lower not in hidden:
                    hidden.append(lower)
                continue
            if lower in user_keys:
                continue
            raise CaptureError(
                "capture-provenance",
                f"unclassified parent authorized_keys path requires explicit trusted classification: {lower}",
            )
        identity = False
        for pattern in _IDENTITY_GLOBS:
            if "*" in pattern:
                if fnmatch.fnmatchcase(lower, pattern) or any(
                    fnmatch.fnmatchcase(prefix, pattern) for prefix in _prefixes(lower)
                ):
                    identity = True
                    break
            else:
                if _under(lower, pattern):
                    identity = True
                    break
        if identity and not any(_under(lower, done) for done in hidden):
            hidden.append(lower)

    writer = _DigestSink(sink, max_bytes)
    emitted = 0
    try:
        with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            items: list[tuple[str, str, UpperEntry | None]] = [(path, "entry", kept[path]) for path in kept]
            items.extend((path, "hide", None) for path in hidden)
            # Sort strictly by the emitted tar member name for deterministic canonical ordering
            items.sort(key=lambda it: _emitted_tar_name(it[0], it[1], it[2]))
            for path, role, entry in items:
                if role == "hide":
                    archive.addfile(_marker(path, _WHITEOUT_PREFIX + posixpath.basename(path)))
                    emitted += 1
                    continue
                assert entry is not None
                emitted += _emit(archive, path, entry, kept, open_payload)
    except CaptureError:
        raise
    except (OSError, tarfile.TarError, ValueError, TypeError):
        raise CaptureError("capture-emit", "layer emission failed") from None
    return CaptureReceipt(
        diff_id=writer.digest,
        size=writer.size,
        entries=emitted,
        excluded=tuple(excluded),
        ignored=tuple(ignored),
        hidden=tuple(hidden),
        retained=tuple(retained),
    )


def _emit(
    archive: tarfile.TarFile,
    path: str,
    entry: UpperEntry,
    kept: dict[str, UpperEntry],
    open_payload: Callable[[object], AbstractContextManager[_Readable]],
) -> int:
    count = 0
    if entry.kind is UpperKind.WHITEOUT:
        archive.addfile(_marker(path, _WHITEOUT_PREFIX + posixpath.basename(path)))
        return 1
    info = tarfile.TarInfo(name=path)
    info.mode = entry.mode & 0o7777
    info.uid, info.gid = entry.uid, entry.gid
    info.mtime = max(entry.mtime, 0)
    xattrs = tuple(item for item in entry.xattrs if item[0] not in _OVERLAY_OPAQUE_NAMES + _OVERLAY_BOOKKEEPING_NAMES)
    info.pax_headers = _pax_xattrs(xattrs)
    if entry.kind is UpperKind.DIRECTORY:
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        count = 1
        if any(name in _OVERLAY_OPAQUE_NAMES for name, _ in entry.xattrs):
            archive.addfile(_marker(path + "/x", _OPAQUE_MARKER))
            count += 1
    elif entry.kind is UpperKind.SYMLINK:
        if not entry.link_target or "\0" in entry.link_target:
            raise CaptureError("capture-symlink", "symlink target is invalid")
        info.type = tarfile.SYMTYPE
        info.linkname = entry.link_target
        archive.addfile(info)
        count = 1
    elif entry.kind is UpperKind.HARDLINK:
        target = normalize_capture_path(entry.link_target)
        if target not in kept:
            raise CaptureError("capture-hardlink", "hardlink target is excluded or not in kept")
        target_entry = kept[target]
        if target_entry.kind is not UpperKind.FILE:
            raise CaptureError("capture-hardlink", "hardlink target must be a regular file")
        if target >= path:
            raise CaptureError("capture-hardlink", "hardlink target is excluded or not emitted earlier")
        info.type = tarfile.LNKTYPE
        info.linkname = target
        archive.addfile(info)
        count = 1
    elif entry.kind is UpperKind.FILE:
        if entry.payload is None:
            raise CaptureError("capture-payload", "regular entry has no payload")
        if entry.size < 0:
            raise CaptureError("capture-payload", "negative payload size is invalid")
        info.type = tarfile.REGTYPE
        info.size = entry.size
        with open_payload(entry.payload) as payload:
            archive.addfile(info, payload)
            if hasattr(payload, "read"):
                trailing = payload.read(1)
                if trailing:
                    raise CaptureError("capture-payload", "payload stream exceeds declared size")
        count = 1
    else:
        raise CaptureError("capture-entry", "unsupported entry kind")
    return count


__all__ = [
    "OCI_CAPTURE_ID",
    "CaptureError",
    "CaptureReceipt",
    "IgnoreMatcher",
    "UpperEntry",
    "UpperKind",
    "convert_upper_to_layer",
    "normalize_capture_path",
    "resolve_commit_plmignore",
]
