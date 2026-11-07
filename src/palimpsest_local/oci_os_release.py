"""Read source-bound effective image OS identity without extracting or executing it.

Only primitive occurrence identities survive a staging context. The selected
payload is read by replaying its origin layer, and that replay consumes the same
cumulative decompression/metadata budgets as the initial pass. Deadlines are
cooperative, not syscall preemption or native-root execution admission.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import deque
from dataclasses import dataclass, replace

from .errors import ArtifactValidationError
from .oci_changeset import (
    ChangesetValidationError,
    EffectiveFilesystem,
    EntryKind,
    NormalizedChangeset,
    NormalizedEntry,
)
from .oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LayerConversionLimits, StagedLayer, stage_layer
from .oci_image import resolve_archive_image, resolve_image
from .oci_provenance import Descriptor
from .oci_source import SnapshottedOCIArchive, SnapshottedOCIImage, SourceCAS, SourceSnapshot


class OSReleaseError(ArtifactValidationError):
    """A source graph, effective path, format or resource bound failed closed."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class OSReadLimits:
    max_layers: int = 128
    max_entries: int = 500_000
    max_uncompressed_bytes: int = 8 * 1024**3
    max_file_bytes: int = 65_536
    max_path_bytes: int = 4096
    max_symlink_traversals: int = 40
    timeout_seconds: float = 300.0
    max_members: int = 500_000
    max_metadata_bytes: int = 64 * 1024**2

    def __post_init__(self) -> None:
        integers = (
            self.max_layers,
            self.max_entries,
            self.max_uncompressed_bytes,
            self.max_file_bytes,
            self.max_path_bytes,
            self.max_symlink_traversals,
            self.max_members,
            self.max_metadata_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in integers):
            raise OSReleaseError("oci-os-limit", "integer limits must be positive")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise OSReleaseError("oci-os-limit", "timeout must be finite and positive")
        try:
            timeout = float(self.timeout_seconds)
        except OverflowError:
            raise OSReleaseError("oci-os-limit", "timeout must be finite and positive") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise OSReleaseError("oci-os-limit", "timeout must be finite and positive")


DEFAULT_OS_READ_LIMITS = OSReadLimits()


@dataclass(frozen=True, slots=True)
class ImageOSRelease:
    source_binding_digest: str
    source_path: str
    resolved_path: str
    layer_ordinal: int
    content_digest: str
    os_id: str
    version_id: str

    def to_dict(self) -> dict[str, str | int]:
        """Describe this observation; this is not a qualification import API."""
        return {
            "source_binding_digest": self.source_binding_digest,
            "source_path": self.source_path,
            "resolved_path": self.resolved_path,
            "layer_ordinal": self.layer_ordinal,
            "content_digest": self.content_digest,
            "os_id": self.os_id,
            "version_id": self.version_id,
        }


@dataclass(frozen=True, slots=True)
class _Origin:
    layer_ordinal: int
    carrier_path: str
    member_index: int
    size: int
    digest: str


class _Budget:
    def __init__(self, limits: OSReadLimits):
        self.limits = limits
        self.expires = time.monotonic() + limits.timeout_seconds
        self.uncompressed = 0
        self.members = 0
        self.metadata = 0

    def check(self) -> None:
        if time.monotonic() >= self.expires:
            raise OSReleaseError("oci-os-timeout", "OS identity read exceeded its cooperative deadline")

    def metadata_bytes(self, amount: int) -> None:
        self.metadata += amount
        if self.metadata > self.limits.max_metadata_bytes:
            raise OSReleaseError("oci-os-metadata-limit", "cumulative metadata exceeds its byte limit")
        self.check()

    def layer_limits(self) -> LayerConversionLimits:
        self.check()
        remaining = self.limits.max_uncompressed_bytes - self.uncompressed
        members = self.limits.max_members - self.members
        metadata = self.limits.max_metadata_bytes - self.metadata
        if remaining <= 0 or members <= 0 or metadata <= 0:
            raise OSReleaseError("oci-os-limit", "cumulative layer budget is exhausted")
        base = DEFAULT_LAYER_CONVERSION_LIMITS
        return replace(
            base,
            max_uncompressed_bytes=min(base.max_uncompressed_bytes, remaining),
            max_compressed_bytes=min(base.max_compressed_bytes, self.limits.max_uncompressed_bytes),
            max_regular_file_bytes=min(base.max_regular_file_bytes, remaining),
            max_total_regular_bytes=min(base.max_total_regular_bytes, remaining),
            max_members=min(base.max_members, members),
            max_normalized_entries=min(base.max_normalized_entries, self.limits.max_entries),
            max_total_metadata_bytes=min(base.max_total_metadata_bytes, metadata),
            max_path_bytes=self.limits.max_path_bytes,
            max_total_xattr_bytes=min(base.max_total_xattr_bytes, metadata),
            timeout_seconds=self.expires - time.monotonic(),
        )

    def record(self, staged: StagedLayer) -> None:
        receipt = staged.receipt
        self.uncompressed += receipt.uncompressed_size
        self.members += receipt.members
        # This is cumulative inspected/retained metadata, not an aggregate PAX
        # counter. The converter bounds full PAX metadata independently per
        # layer; cumulative tar bytes also include every extension/header.
        amount = receipt.xattr_bytes
        for member in staged.members:
            self.check()
            amount += 128 + len(member.path.encode("utf-8")) + len(member.link_target.encode("utf-8"))
        for entry in staged.changeset.entries:
            self.check()
            amount += 256 + len(entry.path.encode("utf-8")) + len(entry.link_target.encode("utf-8"))
            amount += sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in entry.xattrs)
        self.metadata_bytes(amount)


class _Filesystem(EffectiveFilesystem[_Origin]):
    def __init__(self, budget: _Budget):
        super().__init__(
            max_entries=budget.limits.max_entries,
            max_metadata_bytes=budget.limits.max_metadata_bytes,
            checkpoint=budget.check,
        )
        self.budget = budget

    def apply(self, staged: StagedLayer) -> None:
        # Keep only primitive origin identities after each staging context.
        normalized = staged.changeset.by_path()
        entries: list[NormalizedEntry[_Origin]] = []
        for entry in staged.changeset.entries:
            self.budget.check()
            origin = None
            kind = entry.kind
            if kind in {EntryKind.FILE, EntryKind.HARDLINK}:
                carrier = entry if kind is EntryKind.FILE else normalized.get(entry.link_target)
                if carrier is None or carrier.kind is not EntryKind.FILE or carrier.payload is None:
                    raise OSReleaseError("oci-os-hardlink", "normalized hardlink has no regular carrier")
                reference = carrier.payload
                origin = _Origin(
                    staged.receipt.ordinal,
                    carrier.path,
                    reference.member_index,
                    reference.size,
                    reference.digest,
                )
                kind = EntryKind.FILE
            entries.append(replace(entry, kind=kind, payload=origin))
        try:
            super().apply(NormalizedChangeset(tuple(entries)))
        except ChangesetValidationError as exc:
            code = "oci-os-entry-limit" if exc.code == "oci-changeset-entry-limit" else "oci-os-metadata-limit"
            raise OSReleaseError(code, "effective filesystem exceeds its resource limit") from None

    def resolve(self, absolute: str) -> tuple[str, NormalizedEntry[_Origin]] | None:
        # Required components belong to a symlink's target; missing one is a
        # dangling link, not permission to fall back to /usr/lib/os-release.
        pending = deque((part, False) for part in absolute.split("/") if part)
        resolved: list[str] = []
        traversals = 0
        while pending:
            self.budget.check()
            component, required = pending.popleft()
            if component == ".":
                continue
            if component == "..":
                if not resolved:
                    raise OSReleaseError("oci-os-link-escape", "image symlink escapes the image root")
                resolved.pop()
                continue
            path = "/".join((*resolved, component))
            if len(path.encode("utf-8")) + 1 > self.budget.limits.max_path_bytes:
                raise OSReleaseError("oci-os-path-limit", "resolved image path exceeds its byte limit")
            entry = self.entries.get(path)
            if entry is None:
                if required:
                    raise OSReleaseError("oci-os-dangling-link", "OS identity path has a dangling symlink")
                return None
            if entry.kind is EntryKind.SYMLINK:
                traversals += 1
                if traversals > self.budget.limits.max_symlink_traversals:
                    raise OSReleaseError("oci-os-link-limit", "OS identity path exceeds its symlink traversal limit")
                target = entry.link_target
                if target.startswith("/"):
                    resolved.clear()
                target_parts = [(part, True) for part in target.split("/") if part]
                # A trailing slash requires a directory even for the final link.
                if target.endswith("/"):
                    target_parts.append((".", True))
                pending.extendleft(reversed(target_parts))
                continue
            resolved.append(component)
            if pending and entry.kind is not EntryKind.DIRECTORY:
                raise OSReleaseError("oci-os-not-directory", "OS identity path traverses a non-directory")
        path = "/".join(resolved) or "."
        entry = self.entries.get(path)
        if entry is None:
            raise OSReleaseError("oci-os-dangling-link", "OS identity symlink target is missing")
        return ("/" + path if path != "." else "/", entry)


def _verify_graph(snapshot: SnapshottedOCIImage | SnapshottedOCIArchive, cas: SourceCAS, budget: _Budget) -> None:
    def reader(descriptor: Descriptor) -> bytes:
        budget.check()
        payload = cas.read_metadata(SourceSnapshot(descriptor, cas.identity))
        budget.metadata_bytes(len(payload))
        return payload

    image = snapshot.image
    if isinstance(snapshot, SnapshottedOCIArchive):
        actual = resolve_archive_image(
            image.reference,
            snapshot.root.descriptor,
            reader,
            platform=f"{image.config.os}/{image.config.architecture}",
        )
    else:
        actual = resolve_image(image.reference, snapshot.root.descriptor, reader)
    if actual != image:
        raise OSReleaseError("oci-os-graph", "snapshot graph differs from its verified source bytes")
    budget.check()


def _read_origin(staged: StagedLayer, origin: _Origin, budget: _Budget) -> bytes:
    for entry in staged.changeset.entries:
        budget.check()
        if entry.path != origin.carrier_path:
            continue
        ref = entry.payload
        if (
            entry.kind is not EntryKind.FILE
            or ref is None
            or (ref.member_index, ref.size, ref.digest) != (origin.member_index, origin.size, origin.digest)
        ):
            break
        content = bytearray()
        with staged.lease_regular_payload(ref) as lease:
            for chunk in lease.chunks(min(65_536, budget.limits.max_file_bytes)):
                budget.check()
                content.extend(chunk)
                if len(content) > budget.limits.max_file_bytes:
                    raise OSReleaseError("oci-os-file-limit", "OS identity file exceeds its byte limit")
        return bytes(content)
    raise OSReleaseError("oci-os-origin", "replayed layer does not contain the selected payload occurrence")


_KEY = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_UNQUOTED = re.compile(r"[a-zA-Z0-9_./:+,@%=\-]*\Z")
_IDENTITY = re.compile(r"[a-z0-9._-]+\Z")


def _assignment_value(raw: str) -> str:
    if not raw:
        return ""
    if raw[0] not in {"'", '"'}:
        result: list[str] = []
        index = 0
        while index < len(raw):
            char = raw[index]
            if char == "\\":
                index += 1
                if index >= len(raw) or not raw[index].isprintable():
                    raise OSReleaseError("oci-os-format", "assignment contains an unsupported escape")
                char = raw[index]
            elif _UNQUOTED.fullmatch(char) is None:
                raise OSReleaseError("oci-os-format", "unquoted assignment contains unsupported shell syntax")
            result.append(char)
            index += 1
        return "".join(result)
    quote = raw[0]
    if len(raw) < 2 or raw[-1] != quote:
        raise OSReleaseError("oci-os-format", "assignment has unmatched quotes")
    inner = raw[1:-1]
    result = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == quote:
            raise OSReleaseError("oci-os-format", "concatenated quoted fragments are unsupported")
        if quote == '"' and char == "\\":
            index += 1
            if index >= len(inner) or inner[index] not in '\\"$`':
                raise OSReleaseError("oci-os-format", "assignment contains an unsupported escape")
            char = inner[index]
        elif quote == '"' and char in "$`":
            raise OSReleaseError("oci-os-format", "shell expansion is unsupported")
        result.append(char)
        index += 1
    return "".join(result)


def _parse_os_release(payload: bytes) -> tuple[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise OSReleaseError("oci-os-format", "OS identity file is not UTF-8") from None
    if any(not char.isprintable() and char not in "\n\t" for char in text):
        raise OSReleaseError("oci-os-format", "OS identity file contains non-printable data")
    values: dict[str, str] = {}
    for line in text.split("\n"):
        line = line.strip(" \t")
        if not line or line.startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        if not separator or _KEY.fullmatch(key) is None:
            raise OSReleaseError("oci-os-format", "OS identity file contains a non-assignment")
        if key in values:
            raise OSReleaseError("oci-os-format", "OS identity file contains a duplicate key")
        value = _assignment_value(raw)
        if any(not char.isprintable() for char in value):
            raise OSReleaseError("oci-os-format", "assignment value contains non-printable data")
        values[key] = value
    os_id, version_id = values.get("ID", ""), values.get("VERSION_ID", "")
    if _IDENTITY.fullmatch(os_id) is None or _IDENTITY.fullmatch(version_id) is None:
        raise OSReleaseError("oci-os-identity", "valid explicit ID and VERSION_ID assignments are required")
    return os_id, version_id


def read_image_os_release(
    snapshot: SnapshottedOCIImage | SnapshottedOCIArchive,
    cas: SourceCAS,
    *,
    limits: OSReadLimits = DEFAULT_OS_READ_LIMITS,
) -> ImageOSRelease:
    """Verify every ordered source layer and read the effective OS identity."""
    if not isinstance(snapshot, (SnapshottedOCIImage, SnapshottedOCIArchive)) or not isinstance(cas, SourceCAS):
        raise OSReleaseError("oci-os-source", "OS identity read requires a source-CAS image snapshot")
    if not isinstance(limits, OSReadLimits):
        raise OSReleaseError("oci-os-limit", "OS identity read limits are invalid")
    if snapshot.cas_id != cas.identity:
        raise OSReleaseError("oci-os-source", "snapshot belongs to a different source CAS")
    if len(snapshot.layers) > limits.max_layers:
        raise OSReleaseError("oci-os-layer-limit", "image exceeds its ordered layer limit")
    if sum(layer.descriptor.size for layer in snapshot.layers) > limits.max_uncompressed_bytes:
        raise OSReleaseError("oci-os-compressed-limit", "compressed source occurrences exceed the byte limit")
    budget = _Budget(limits)
    _verify_graph(snapshot, cas, budget)
    filesystem = _Filesystem(budget)
    for ordinal in range(len(snapshot.layers)):
        with cas.lease_layer(snapshot, ordinal) as source:
            with stage_layer(source, limits=budget.layer_limits()) as staged:
                budget.record(staged)
                filesystem.apply(staged)
    source_path = "/etc/os-release"
    selected = filesystem.resolve(source_path)
    if selected is None:
        source_path = "/usr/lib/os-release"
        selected = filesystem.resolve(source_path)
    if selected is None:
        raise OSReleaseError("oci-os-missing", "image has no effective os-release file")
    resolved_path, entry = selected
    if entry.kind is not EntryKind.FILE or entry.payload is None:
        raise OSReleaseError("oci-os-not-regular", "effective OS identity path is not a regular file")
    origin = entry.payload
    if origin.size > limits.max_file_bytes:
        raise OSReleaseError("oci-os-file-limit", "OS identity file exceeds its byte limit")
    with cas.lease_layer(snapshot, origin.layer_ordinal) as source:
        with stage_layer(source, limits=budget.layer_limits()) as staged:
            budget.record(staged)
            payload = _read_origin(staged, origin, budget)
    os_id, version_id = _parse_os_release(payload)
    if f"sha256:{hashlib.sha256(payload).hexdigest()}" != origin.digest:
        raise OSReleaseError("oci-os-origin", "selected OS identity content digest changed")
    # Recheck every occurrence, not just the selected origin. No spool or image
    # path is retained, and the bounded chunk loop observes the total deadline.
    for ordinal in range(len(snapshot.layers)):
        with cas.lease_layer(snapshot, ordinal) as source:
            for _chunk in source.chunks():
                budget.check()
    _verify_graph(snapshot, cas, budget)
    return ImageOSRelease(
        snapshot.binding_digest,
        source_path,
        resolved_path,
        origin.layer_ordinal,
        origin.digest,
        os_id,
        version_id,
    )


__all__ = [
    "DEFAULT_OS_READ_LIMITS",
    "ImageOSRelease",
    "OSReadLimits",
    "OSReleaseError",
    "read_image_os_release",
]
