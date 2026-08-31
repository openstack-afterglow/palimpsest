"""Deterministic streaming tar emission for normalized OCI changesets."""

from __future__ import annotations

import hashlib
import tarfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from .errors import ArtifactValidationError
from .oci_changeset import EntryKind, NormalizedChangeset, NormalizedEntry

OCI_NORMALIZED_TAR_EMISSION_ID = "palimpsest.oci-normalized-tar-emission.v1"


class _ReadablePayload(Protocol):
    def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class TarEmissionReceipt:
    """Path-free evidence for one deterministic normalized tar stream."""

    digest: str
    size: int
    entries: int


class _HashingWriter:
    __slots__ = ("_digest", "_maximum", "_sink", "size")

    def __init__(self, sink: BinaryIO, maximum: int | None) -> None:
        self._sink = sink
        self._maximum = maximum
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, payload: bytes) -> int:
        if not isinstance(payload, bytes):
            raise ArtifactValidationError("Normalized tar emitter produced a non-bytes chunk")
        if self._maximum is not None and len(payload) > self._maximum - self.size:
            raise ArtifactValidationError("Normalized tar output exceeds its byte limit")
        try:
            written = self._sink.write(payload)
        except OSError:
            raise ArtifactValidationError("Normalized tar output could not be written") from None
        if written != len(payload):
            raise ArtifactValidationError("Normalized tar output write was incomplete")
        self._digest.update(payload)
        self.size += len(payload)
        return written

    def flush(self) -> None:
        try:
            self._sink.flush()
        except OSError:
            raise ArtifactValidationError("Normalized tar output could not be flushed") from None

    @property
    def digest(self) -> str:
        return f"sha256:{self._digest.hexdigest()}"


def normalized_xattr_bytes(name: str, value: str) -> bytes:
    """Recover the reviewed byte encoding for one normalized xattr value."""
    try:
        return value.encode("latin-1") if name == "security.capability" else value.encode("utf-8")
    except UnicodeEncodeError:
        raise ArtifactValidationError("Normalized xattr value cannot be encoded losslessly") from None


def _pax_xattr_value(name: str, value: str) -> str:
    raw = normalized_xattr_bytes(name, value)
    return raw.decode("utf-8", errors="surrogateescape")


def _tar_info(entry: NormalizedEntry[object]) -> tarfile.TarInfo:
    tar_types = {
        EntryKind.FILE: tarfile.REGTYPE,
        EntryKind.DIRECTORY: tarfile.DIRTYPE,
        EntryKind.HARDLINK: tarfile.LNKTYPE,
        EntryKind.SYMLINK: tarfile.SYMTYPE,
        EntryKind.CHAR: tarfile.CHRTYPE,
        EntryKind.BLOCK: tarfile.BLKTYPE,
        EntryKind.FIFO: tarfile.FIFOTYPE,
        EntryKind.WHITEOUT: tarfile.CHRTYPE,
    }
    info = tarfile.TarInfo(name=entry.path)
    info.type = tar_types[entry.kind]
    info.mode = entry.mode
    info.uid = entry.uid
    info.gid = entry.gid
    info.size = entry.size
    info.mtime = entry.mtime
    info.linkname = entry.link_target
    info.devmajor = entry.device_major
    info.devminor = entry.device_minor
    if entry.kind is EntryKind.WHITEOUT:
        return info
    info.uname = str(entry.uid)
    info.gname = str(entry.gid)
    pax_headers = {f"SCHILY.xattr.{key}": _pax_xattr_value(key, value) for key, value in entry.xattrs}
    pax_headers.setdefault("uid", str(entry.uid))
    pax_headers.setdefault("gid", str(entry.gid))
    info.pax_headers = pax_headers
    return info


def emit_normalized_overlay_tar[PayloadT](
    changeset: NormalizedChangeset[PayloadT],
    sink: BinaryIO,
    open_payload: Callable[[PayloadT], AbstractContextManager[_ReadablePayload]],
    *,
    max_bytes: int | None = None,
) -> TarEmissionReceipt:
    """Write one normalized changeset without materializing regular payloads."""
    if not isinstance(changeset, NormalizedChangeset):
        raise ArtifactValidationError("Normalized tar emission requires a normalized changeset")
    if not callable(open_payload) or not hasattr(sink, "write"):
        raise ArtifactValidationError("Normalized tar emission boundary is invalid")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise ArtifactValidationError("Normalized tar output byte limit is invalid")

    writer = _HashingWriter(sink, max_bytes)
    try:
        with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for entry in changeset.entries:
                info = _tar_info(entry)
                if entry.kind is EntryKind.FILE:
                    if entry.payload is None:
                        raise ArtifactValidationError("Normalized regular entry has no payload reference")
                    with open_payload(entry.payload) as payload:
                        archive.addfile(info, payload)
                else:
                    archive.addfile(info)
    except ArtifactValidationError:
        raise
    except (OSError, tarfile.TarError, ValueError, TypeError):
        raise ArtifactValidationError("Normalized tar emission failed") from None
    return TarEmissionReceipt(digest=writer.digest, size=writer.size, entries=len(changeset.entries))


__all__ = ["TarEmissionReceipt", "emit_normalized_overlay_tar", "normalized_xattr_bytes"]
