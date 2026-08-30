"""Bounded first-pass intake for occurrence-bound OCI layer leases.

This module deliberately stops before filesystem packing or derived-CAS
publication.  It turns a verified compressed source capability into a private,
validated uncompressed-tar capability without materializing the layer in Python
memory or exposing a scratch path/file descriptor to callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import BinaryIO

from .errors import ArtifactValidationError
from .oci_provenance import (
    DOCKER_LAYER_GZIP_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
)
from .oci_source import LeasedSourceLayer

_BLOCK = 512
_ZERO_BLOCK = b"\0" * _BLOCK
_IO_CHUNK = 1024 * 1024

LAYER_INTAKE_POLICY_ID = "palimpsest.oci-layer-intake.v1"


class LayerIntakeError(ArtifactValidationError):
    """A stable, path-free failure from the OCI layer intake boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class LayerConversionLimits:
    """Versioned fail-closed limits applied before any packer can run.

    ``timeout_seconds`` is a cooperative deadline checked between bounded
    chunks and before/after local spool operations.  It does not claim to
    preempt a kernel I/O syscall; the later converter-worker activation owns
    the hard wall-clock kill boundary.
    """

    max_members: int = 250_000
    max_physical_headers: int = 1_000_000
    max_compressed_bytes: int = 32 * 1024**3
    max_uncompressed_bytes: int = 32 * 1024**3
    max_path_bytes: int = 4096
    max_regular_file_bytes: int = 32 * 1024**3
    max_total_regular_bytes: int = 32 * 1024**3
    max_metadata_bytes_per_header: int = 1024 * 1024
    max_total_metadata_bytes: int = 64 * 1024**2
    max_xattr_bytes_per_member: int = 1024 * 1024
    max_total_xattr_bytes: int = 64 * 1024**2
    max_compression_ratio: int = 2048
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        integer_fields = (
            self.max_members,
            self.max_physical_headers,
            self.max_compressed_bytes,
            self.max_uncompressed_bytes,
            self.max_path_bytes,
            self.max_regular_file_bytes,
            self.max_total_regular_bytes,
            self.max_metadata_bytes_per_header,
            self.max_total_metadata_bytes,
            self.max_xattr_bytes_per_member,
            self.max_total_xattr_bytes,
            self.max_compression_ratio,
        )
        if any(type(value) is not int or value <= 0 for value in integer_fields):
            raise LayerIntakeError("oci-limit-policy", "integer limits must be positive")
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(self.timeout_seconds, bool):
            raise LayerIntakeError("oci-limit-policy", "timeout must be a positive number")
        if not 0 < float(self.timeout_seconds) < float("inf"):
            raise LayerIntakeError("oci-limit-policy", "timeout must be finite and positive")
        if self.max_physical_headers < self.max_members:
            raise LayerIntakeError("oci-limit-policy", "physical-header limit cannot be below member limit")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "domain": LAYER_INTAKE_POLICY_ID,
                "max_compressed_bytes": self.max_compressed_bytes,
                "max_compression_ratio": self.max_compression_ratio,
                "max_members": self.max_members,
                "max_metadata_bytes_per_header": self.max_metadata_bytes_per_header,
                "max_path_bytes": self.max_path_bytes,
                "max_physical_headers": self.max_physical_headers,
                "max_regular_file_bytes": self.max_regular_file_bytes,
                "max_total_metadata_bytes": self.max_total_metadata_bytes,
                "max_total_regular_bytes": self.max_total_regular_bytes,
                "max_total_xattr_bytes": self.max_total_xattr_bytes,
                "max_uncompressed_bytes": self.max_uncompressed_bytes,
                "max_xattr_bytes_per_member": self.max_xattr_bytes_per_member,
                "timeout_seconds": float(self.timeout_seconds),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


DEFAULT_LAYER_CONVERSION_LIMITS = LayerConversionLimits()


@dataclass(frozen=True, slots=True)
class PhysicalLayerMember:
    """One validated physical member; archive normalization happens next slice."""

    path: str
    kind: str
    size: int
    mode: int
    uid: int
    gid: int
    mtime: int
    link_target: str
    device_major: int
    device_minor: int
    xattrs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class LayerIntakeReceipt:
    """Path-free evidence for one completed first-pass layer intake."""

    policy_id: str
    policy_fingerprint: str
    ordinal: int
    media_type: str
    compressed_digest: str
    compressed_size: int
    diff_id: str
    uncompressed_size: int
    physical_headers: int
    members: int
    regular_bytes: int
    xattr_bytes: int


class _Deadline:
    __slots__ = ("_clock", "_expires")

    def __init__(self, clock: Callable[[], float], seconds: float):
        self._clock = clock
        self._expires = clock() + seconds

    def check(self) -> None:
        if self._clock() > self._expires:
            raise LayerIntakeError("oci-layer-timeout", "layer intake exceeded its cooperative deadline")


def _read_exact_at(spool: BinaryIO, offset: int, size: int) -> bytes:
    try:
        value = os.pread(spool.fileno(), size, offset)
    except OSError:
        raise LayerIntakeError("oci-tar-io", "cannot read private layer spool") from None
    if len(value) != size:
        raise LayerIntakeError("oci-tar-framing", "tar member is truncated")
    return value


def _tar_number(field: bytes, label: str) -> int:
    if field and field[0] & 0x80:
        if field[0] & 0x40:
            raise LayerIntakeError("oci-tar-metadata", f"negative {label} is unsupported")
        value = int.from_bytes(bytes((field[0] & 0x7F,)) + field[1:], "big")
        if value < 0:
            raise LayerIntakeError("oci-tar-metadata", f"negative {label} is unsupported")
        return value
    raw = field.strip(b"\0 ")
    try:
        value = int(raw or b"0", 8)
    except ValueError:
        raise LayerIntakeError("oci-tar-metadata", f"invalid {label}") from None
    if value < 0:
        raise LayerIntakeError("oci-tar-metadata", f"negative {label} is unsupported")
    return value


def _pax_integer(pax: dict[str, str], key: str, fallback: int, maximum: int) -> int:
    raw = pax.get(key)
    if raw is None:
        return fallback
    grammar = r"[0-9]+(?:\.[0-9]+)?" if key == "mtime" else r"[0-9]+"
    if re.fullmatch(grammar, raw) is None:
        raise LayerIntakeError("oci-pax-metadata", f"PAX {key} has invalid numeric syntax")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise LayerIntakeError("oci-pax-metadata", f"PAX {key} is not numeric") from None
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise LayerIntakeError("oci-pax-metadata", f"PAX {key} must be a nonnegative integer")
    if value > maximum:
        raise LayerIntakeError("oci-pax-metadata", f"PAX {key} exceeds its supported range")
    return int(value)


def _tar_text(field: bytes, label: str) -> str:
    raw, separator, padding = field.partition(b"\0")
    if separator and padding.strip(b"\0 "):
        raise LayerIntakeError("oci-tar-metadata", f"invalid {label} padding")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LayerIntakeError("oci-tar-utf8", f"{label} is not valid UTF-8") from None


def _validate_path(path: str, limit: int) -> str:
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        raise LayerIntakeError("oci-tar-utf8", "member path is not valid UTF-8") from None
    if len(encoded) > limit:
        raise LayerIntakeError("oci-path-limit", "member path exceeds the byte limit")
    if not path or path == ".":
        return "."
    if "\0" in path or "\\" in path or ".." in path.split("/"):
        raise LayerIntakeError("oci-invalid-path", "member path is unsafe")
    normalized = os.path.normpath(path)
    if normalized.startswith("/") or normalized in {"..", "."} or normalized.startswith("../"):
        if normalized == "." and path.strip("./") == "":
            return "."
        raise LayerIntakeError("oci-invalid-path", "member path is unsafe")
    if normalized == ".palimpsest" or normalized.startswith(".palimpsest/"):
        raise LayerIntakeError("oci-reserved-path", "member uses the reserved Palimpsest tree")
    return normalized


def _parse_pax(payload: bytes) -> dict[str, str]:
    records: dict[str, str] = {}
    offset = 0
    while offset < len(payload):
        space = payload.find(b" ", offset)
        if space <= offset:
            raise LayerIntakeError("oci-pax-metadata", "PAX record length is malformed")
        try:
            length = int(payload[offset:space])
        except ValueError:
            raise LayerIntakeError("oci-pax-metadata", "PAX record length is malformed") from None
        end = offset + length
        if length <= space - offset + 2 or end > len(payload) or payload[end - 1 : end] != b"\n":
            raise LayerIntakeError("oci-pax-metadata", "PAX record framing is malformed")
        record = payload[space + 1 : end - 1]
        key_raw, separator, value_raw = record.partition(b"=")
        if not separator or not key_raw or b"\0" in record:
            raise LayerIntakeError("oci-pax-metadata", "PAX record is malformed")
        try:
            key = key_raw.decode("utf-8")
            value = value_raw.decode("utf-8")
        except UnicodeDecodeError:
            raise LayerIntakeError("oci-tar-utf8", "PAX metadata is not valid UTF-8") from None
        records[key] = value
        offset = end
    return records


def _xattrs(pax: dict[str, str], limits: LayerConversionLimits) -> tuple[tuple[tuple[str, str], ...], int]:
    values: list[tuple[str, str]] = []
    total = 0
    for key, value in pax.items():
        if key.startswith("LIBARCHIVE.xattr.") or key.startswith("SCHILY.xattr.trusted."):
            raise LayerIntakeError("oci-xattr-policy", "reserved xattr encoding is unsupported")
        if not key.startswith("SCHILY.xattr."):
            continue
        name = key.removeprefix("SCHILY.xattr.")
        if not (name.startswith("user.") or name == "security.capability"):
            raise LayerIntakeError("oci-xattr-policy", "xattr namespace is unsupported")
        if any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for character in name
        ):
            raise LayerIntakeError("oci-xattr-policy", "xattr name is invalid")
        if name == "security.capability":
            try:
                raw_capability = value.encode("latin-1")
            except UnicodeEncodeError:
                raise LayerIntakeError("oci-xattr-policy", "capability metadata is invalid") from None
            if len(raw_capability) not in {12, 20, 24}:
                raise LayerIntakeError("oci-xattr-policy", "capability metadata is invalid")
            magic = int.from_bytes(raw_capability[:4], "little")
            expected = {0x01000000: 12, 0x02000000: 20, 0x03000000: 24}.get(magic & 0xFF000000)
            if expected != len(raw_capability) or magic & 0x00FFFFFE:
                raise LayerIntakeError("oci-xattr-policy", "capability metadata is invalid")
        encoded_size = len(key.encode("utf-8")) + len(value.encode("utf-8"))
        total += encoded_size
        values.append((name, value))
    if total > limits.max_xattr_bytes_per_member:
        raise LayerIntakeError("oci-xattr-limit", "member xattrs exceed the byte limit")
    return tuple(sorted(values)), total


def _scan_tar(
    spool: BinaryIO,
    size: int,
    limits: LayerConversionLimits,
    deadline: _Deadline,
) -> tuple[tuple[PhysicalLayerMember, ...], int, int, int, int]:
    if size < 2 * _BLOCK or size % _BLOCK:
        raise LayerIntakeError("oci-tar-framing", "tar size is not block aligned")
    offset = 0
    physical_headers = 0
    metadata_bytes = 0
    regular_bytes = 0
    total_xattr_bytes = 0
    members: list[PhysicalLayerMember] = []
    global_pax: dict[str, str] = {}
    pending_pax: dict[str, str] | None = None
    pending_long_name: str | None = None
    pending_long_link: str | None = None

    while offset + _BLOCK <= size:
        deadline.check()
        header = _read_exact_at(spool, offset, _BLOCK)
        if header == _ZERO_BLOCK:
            if offset + 2 * _BLOCK > size or _read_exact_at(spool, offset + _BLOCK, _BLOCK) != _ZERO_BLOCK:
                raise LayerIntakeError("oci-tar-trailer", "tar requires two zero end blocks")
            trailing = offset + 2 * _BLOCK
            while trailing < size:
                deadline.check()
                chunk = _read_exact_at(spool, trailing, min(_IO_CHUNK, size - trailing))
                if any(chunk):
                    raise LayerIntakeError("oci-tar-trailer", "tar has nonzero bytes after its end marker")
                trailing += len(chunk)
            if pending_pax is not None or pending_long_name is not None or pending_long_link is not None:
                raise LayerIntakeError("oci-tar-metadata", "tar ends with unattached extension metadata")
            return tuple(members), physical_headers, regular_bytes, total_xattr_bytes, metadata_bytes

        physical_headers += 1
        if physical_headers > limits.max_physical_headers:
            raise LayerIntakeError("oci-header-limit", "tar exceeds the physical-header limit")
        checksum = _tar_number(header[148:156], "checksum")
        checksum_header = header[:148] + b" " * 8 + header[156:]
        if sum(checksum_header) != checksum:
            raise LayerIntakeError("oci-tar-checksum", "tar header checksum is invalid")
        header_member_size = _tar_number(header[124:136], "member size")
        data_offset = offset + _BLOCK
        type_flag = header[156:157]
        if type_flag in {b"x", b"g", b"L", b"K"}:
            member_size = header_member_size
        else:
            framing_pax = dict(global_pax)
            if pending_pax is not None:
                framing_pax.update(pending_pax)
            member_size = _pax_integer(
                framing_pax,
                "size",
                header_member_size,
                limits.max_uncompressed_bytes,
            )
        padded_size = ((member_size + _BLOCK - 1) // _BLOCK) * _BLOCK
        next_offset = data_offset + padded_size
        if next_offset > size:
            raise LayerIntakeError("oci-tar-framing", "tar member is truncated")

        if type_flag in {b"x", b"g", b"L", b"K"}:
            if member_size > limits.max_metadata_bytes_per_header:
                raise LayerIntakeError("oci-metadata-limit", "tar extension header exceeds its byte limit")
            metadata_bytes += member_size
            if metadata_bytes > limits.max_total_metadata_bytes:
                raise LayerIntakeError("oci-metadata-limit", "tar extension metadata exceeds its layer limit")
            payload = _read_exact_at(spool, data_offset, member_size)
            if type_flag == b"g":
                global_pax.update(_parse_pax(payload))
            elif type_flag == b"x":
                if pending_pax is not None:
                    raise LayerIntakeError("oci-pax-metadata", "multiple local PAX headers are unsupported")
                pending_pax = _parse_pax(payload)
            else:
                try:
                    decoded = payload.rstrip(b"\0\n").decode("utf-8")
                except UnicodeDecodeError:
                    raise LayerIntakeError("oci-tar-utf8", "GNU long metadata is not valid UTF-8") from None
                if not decoded:
                    raise LayerIntakeError("oci-tar-metadata", "GNU long metadata is empty")
                if type_flag == b"L":
                    pending_long_name = decoded
                else:
                    pending_long_link = decoded
            offset = next_offset
            continue

        if len(members) >= limits.max_members:
            raise LayerIntakeError("oci-member-limit", "tar exceeds the member-count limit")
        prefix = _tar_text(header[345:500], "path prefix")
        header_name = _tar_text(header[0:100], "member path")
        name = f"{prefix}/{header_name}" if prefix else header_name
        link_name = _tar_text(header[157:257], "link target")
        pax = dict(global_pax)
        if pending_pax is not None:
            pax.update(pending_pax)
        if any(key.startswith("GNU.sparse.") or key == "SCHILY.realsize" for key in pax):
            raise LayerIntakeError("oci-sparse", "sparse file metadata is unsupported")
        name = pax.get("path", pending_long_name or name)
        link_name = pax.get("linkpath", pending_long_link or link_name)
        pending_pax = None
        pending_long_name = None
        pending_long_link = None

        normalized = _validate_path(name, limits.max_path_bytes)
        basename = os.path.basename(normalized)
        parent = os.path.dirname(normalized)
        if basename.startswith(".wh.") and basename != ".wh..wh..opq":
            target_name = basename.removeprefix(".wh.")
            if target_name in {"", ".", ".."}:
                raise LayerIntakeError("oci-whiteout", "whiteout target name is invalid")
            target = os.path.join(parent, target_name) if parent else target_name
            _validate_path(target, limits.max_path_bytes)

        mode = _tar_number(header[100:108], "mode")
        uid = _pax_integer(pax, "uid", _tar_number(header[108:116], "uid"), 0xFFFFFFFF)
        gid = _pax_integer(pax, "gid", _tar_number(header[116:124], "gid"), 0xFFFFFFFF)
        mtime = _pax_integer(pax, "mtime", _tar_number(header[136:148], "mtime"), 0xFFFFFFFF)
        if mode > 0o7777 or uid > 0xFFFFFFFF or gid > 0xFFFFFFFF or mtime > 0xFFFFFFFF:
            raise LayerIntakeError("oci-numeric-metadata", "tar numeric metadata is out of range")

        kinds = {
            b"\0": "file",
            b"0": "file",
            b"1": "hardlink",
            b"2": "symlink",
            b"3": "char",
            b"4": "block",
            b"5": "directory",
            b"6": "fifo",
        }
        kind = kinds.get(type_flag)
        if kind is None:
            raise LayerIntakeError("oci-member-type", "tar member type is unsupported")
        if normalized == "." and kind != "directory":
            raise LayerIntakeError("oci-root-metadata", "archive root member must be a directory")
        if basename == ".wh..wh..opq" or basename.startswith(".wh."):
            allowed_whiteout_pax = {"atime", "ctime", "mtime", "path"}
            if kind != "file" or member_size != 0 or set(pax) - allowed_whiteout_pax:
                raise LayerIntakeError(
                    "oci-whiteout",
                    "whiteout marker must be an empty regular file without control metadata",
                )
        if kind == "hardlink":
            link_name = _validate_path(link_name, limits.max_path_bytes)
        elif kind == "symlink":
            if not link_name or "\0" in link_name:
                raise LayerIntakeError("oci-invalid-link", "symlink target is invalid")
            if len(link_name.encode("utf-8")) > limits.max_path_bytes:
                raise LayerIntakeError("oci-path-limit", "symlink target exceeds the byte limit")

        device_major = _tar_number(header[329:337], "device major")
        device_minor = _tar_number(header[337:345], "device minor")
        if kind in {"char", "block"} and not (device_major <= 4095 and device_minor <= 1_048_575):
            raise LayerIntakeError("oci-device-metadata", "device metadata is out of range")
        if kind == "file":
            if member_size > limits.max_regular_file_bytes:
                raise LayerIntakeError("oci-regular-limit", "regular file exceeds the per-file limit")
            regular_bytes += member_size
            if regular_bytes > limits.max_total_regular_bytes:
                raise LayerIntakeError("oci-regular-limit", "regular files exceed the layer byte limit")
        elif member_size != 0:
            raise LayerIntakeError("oci-tar-metadata", "non-regular member has a data payload")

        member_xattrs, xattr_size = _xattrs(pax, limits)
        total_xattr_bytes += xattr_size
        if total_xattr_bytes > limits.max_total_xattr_bytes:
            raise LayerIntakeError("oci-xattr-limit", "layer xattrs exceed the byte limit")
        members.append(
            PhysicalLayerMember(
                path=normalized,
                kind=kind,
                size=member_size,
                mode=mode,
                uid=uid,
                gid=gid,
                mtime=mtime,
                link_target=link_name,
                device_major=device_major,
                device_minor=device_minor,
                xattrs=member_xattrs,
            )
        )
        offset = next_offset
    raise LayerIntakeError("oci-tar-trailer", "tar end marker is missing")


class StagedLayer:
    """Opaque validated tar capability owned by ``stage_layer``'s context."""

    __slots__ = (
        "_closed",
        "_members",
        "_owner_pid",
        "_owner_thread",
        "_receipt",
        "_spool",
        "_started",
    )

    def __init__(
        self,
        spool: BinaryIO,
        receipt: LayerIntakeReceipt,
        members: tuple[PhysicalLayerMember, ...],
    ) -> None:
        self._spool = spool
        self._closed = False
        self._started = False
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._receipt = receipt
        self._members = members

    @property
    def receipt(self) -> LayerIntakeReceipt:
        return self._receipt

    @property
    def members(self) -> tuple[PhysicalLayerMember, ...]:
        return self._members

    def chunks(self, chunk_size: int = _IO_CHUNK) -> Iterator[bytes]:
        if os.getpid() != self._owner_pid or threading.get_ident() != self._owner_thread:
            raise LayerIntakeError("oci-stage-owner", "staged layer cannot cross a process or thread boundary")
        if self._closed:
            raise LayerIntakeError("oci-stage-closed", "staged layer is closed")
        if type(chunk_size) is not int or not 1 <= chunk_size <= _IO_CHUNK:
            raise LayerIntakeError("oci-stage-chunk", "staged layer chunk size is out of range")
        if self._started:
            raise LayerIntakeError("oci-stage-consumed", "staged layer stream is single-use")
        self._started = True
        try:
            self._spool.seek(0)
        except OSError:
            raise LayerIntakeError("oci-stage-io", "cannot rewind private staged layer") from None
        while True:
            try:
                payload = self._spool.read(chunk_size)
            except OSError:
                raise LayerIntakeError("oci-stage-io", "cannot read private staged layer") from None
            if not payload:
                return
            yield payload

    def _close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._spool.close()
            except OSError:
                pass

    def __copy__(self) -> StagedLayer:
        raise LayerIntakeError("oci-stage-copy", "staged layer cannot be copied")

    def __deepcopy__(self, _memo: object) -> StagedLayer:
        raise LayerIntakeError("oci-stage-copy", "staged layer cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("staged layer cannot be serialized")

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"StagedLayer(receipt={self.receipt!r}, members={len(self.members)}, state={state!r})"


def _write_uncompressed(
    spool: BinaryIO,
    payload: bytes,
    hasher: hashlib._Hash,
    total: int,
    compressed_size: int,
    limits: LayerConversionLimits,
) -> int:
    next_total = total + len(payload)
    ratio_limit = limits.max_compression_ratio * max(compressed_size, 1)
    if next_total > limits.max_uncompressed_bytes or next_total > ratio_limit:
        code = "oci-expansion-limit" if next_total > ratio_limit else "oci-uncompressed-limit"
        raise LayerIntakeError(code, "uncompressed layer exceeds its intake limit")
    try:
        written = spool.write(payload)
    except OSError:
        raise LayerIntakeError("oci-spool-io", "cannot write private layer spool") from None
    if written != len(payload):
        raise LayerIntakeError("oci-spool-io", "private layer spool write was incomplete")
    hasher.update(payload)
    return next_total


@contextmanager
def stage_layer(
    lease: LeasedSourceLayer,
    *,
    limits: LayerConversionLimits = DEFAULT_LAYER_CONVERSION_LIMITS,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[StagedLayer]:
    """Decompress, DiffID-check and scan one occurrence-bound source lease."""
    if not isinstance(lease, LeasedSourceLayer):
        raise LayerIntakeError("oci-source-lease", "layer intake requires a leased source occurrence")
    if not isinstance(limits, LayerConversionLimits) or not callable(clock):
        raise LayerIntakeError("oci-limit-policy", "layer intake policy or clock is invalid")
    if lease.compressed_size > limits.max_compressed_bytes:
        raise LayerIntakeError("oci-compressed-limit", "compressed layer exceeds its intake limit")
    if lease.media_type not in {OCI_LAYER_MEDIA_TYPE, OCI_LAYER_GZIP_MEDIA_TYPE, DOCKER_LAYER_GZIP_MEDIA_TYPE}:
        raise LayerIntakeError("oci-codec", "layer media type has no supported codec")

    deadline = _Deadline(clock, float(limits.timeout_seconds))
    try:
        spool = tempfile.TemporaryFile(mode="w+b")
    except OSError:
        raise LayerIntakeError("oci-spool-io", "cannot create private layer spool") from None
    staged: StagedLayer | None = None
    try:
        spool_metadata = os.fstat(spool.fileno())
        if (
            not stat.S_ISREG(spool_metadata.st_mode)
            or spool_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(spool_metadata.st_mode) & 0o077
            or spool_metadata.st_nlink not in {0, 1}
        ):
            raise LayerIntakeError("oci-spool-safety", "private layer spool is unsafe")

        diff_hasher = hashlib.sha256()
        compressed_seen = 0
        uncompressed_seen = 0
        gzip_codec = lease.media_type in {OCI_LAYER_GZIP_MEDIA_TYPE, DOCKER_LAYER_GZIP_MEDIA_TYPE}
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzip_codec else None
        saw_prefix = bytearray()

        for chunk in lease.chunks():
            deadline.check()
            compressed_seen += len(chunk)
            if compressed_seen > limits.max_compressed_bytes:
                raise LayerIntakeError("oci-compressed-limit", "compressed layer exceeds its intake limit")
            if len(saw_prefix) < 4:
                saw_prefix.extend(chunk[: 4 - len(saw_prefix)])
            if decoder is None:
                uncompressed_seen = _write_uncompressed(
                    spool, chunk, diff_hasher, uncompressed_seen, lease.compressed_size, limits
                )
                continue
            pending = chunk
            while pending:
                deadline.check()
                remaining = (
                    min(
                        limits.max_uncompressed_bytes,
                        limits.max_compression_ratio * max(lease.compressed_size, 1),
                    )
                    - uncompressed_seen
                )
                try:
                    output = decoder.decompress(pending, min(_IO_CHUNK, max(1, remaining + 1)))
                except zlib.error:
                    raise LayerIntakeError("oci-gzip", "gzip stream is corrupt or mismatched") from None
                pending = decoder.unconsumed_tail
                uncompressed_seen = _write_uncompressed(
                    spool, output, diff_hasher, uncompressed_seen, lease.compressed_size, limits
                )
                if decoder.unused_data:
                    # Phase 1 binds one descriptor to one gzip member.  RFC
                    # concatenation is an explicit unsupported subset so an
                    # appended second stream cannot alter codec semantics.
                    raise LayerIntakeError("oci-gzip-trailing", "concatenated or trailing gzip data is unsupported")

        deadline.check()
        if decoder is None:
            if bytes(saw_prefix).startswith(b"\x1f\x8b"):
                raise LayerIntakeError("oci-codec", "plain-tar media type contains gzip bytes")
        else:
            if not decoder.eof:
                raise LayerIntakeError("oci-gzip", "gzip stream is truncated")
            try:
                tail = decoder.flush()
            except zlib.error:
                raise LayerIntakeError("oci-gzip", "gzip trailer is corrupt") from None
            uncompressed_seen = _write_uncompressed(
                spool, tail, diff_hasher, uncompressed_seen, lease.compressed_size, limits
            )
            if decoder.unused_data or decoder.unconsumed_tail:
                raise LayerIntakeError("oci-gzip-trailing", "concatenated or trailing gzip data is unsupported")

        actual_diff_id = f"sha256:{diff_hasher.hexdigest()}"
        if actual_diff_id != lease.diff_id:
            raise LayerIntakeError("oci-diffid", "uncompressed layer digest does not match its occurrence")
        try:
            spool.flush()
            os.fsync(spool.fileno())
        except OSError:
            raise LayerIntakeError("oci-spool-io", "cannot durably stage private layer spool") from None

        members, physical_headers, regular_bytes, xattr_bytes, _metadata_bytes = _scan_tar(
            spool, uncompressed_seen, limits, deadline
        )
        receipt = LayerIntakeReceipt(
            policy_id=LAYER_INTAKE_POLICY_ID,
            policy_fingerprint=limits.fingerprint,
            ordinal=lease.ordinal,
            media_type=lease.media_type,
            compressed_digest=lease.compressed_digest,
            compressed_size=compressed_seen,
            diff_id=actual_diff_id,
            uncompressed_size=uncompressed_seen,
            physical_headers=physical_headers,
            members=len(members),
            regular_bytes=regular_bytes,
            xattr_bytes=xattr_bytes,
        )
        staged = StagedLayer(spool, receipt, members)
        yield staged
    finally:
        if staged is not None:
            staged._close()
        else:
            try:
                spool.close()
            except OSError:
                pass


__all__ = [
    "DEFAULT_LAYER_CONVERSION_LIMITS",
    "LAYER_INTAKE_POLICY_ID",
    "LayerConversionLimits",
    "LayerIntakeError",
    "LayerIntakeReceipt",
    "PhysicalLayerMember",
    "StagedLayer",
    "stage_layer",
]
