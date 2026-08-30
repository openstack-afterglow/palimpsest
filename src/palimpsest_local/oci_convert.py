"""OCI layer changeset conversion and filesystem semantics probing for Palimpsest."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError, UnsupportedPlatformError

_PACK_TIMEOUT_SECONDS = 120
_MOUNT_TIMEOUT_SECONDS = 30
_EROFS_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class TarTranslationLimits:
    """Resource limits applied before any privileged filesystem packer runs."""

    max_members: int = 10_000
    max_input_bytes: int = 512 * 1024**2
    max_path_bytes: int = 4096
    max_regular_file_bytes: int = 256 * 1024**2
    max_total_regular_bytes: int = 512 * 1024**2
    max_pax_bytes_per_member: int = 64 * 1024


DEFAULT_TAR_TRANSLATION_LIMITS = TarTranslationLimits()


class FilesystemCandidate(StrEnum):
    """Explicit immutable-layer candidates evaluated by the PR 2 probe."""

    SQUASHFS = "squashfs"
    EROFS = "erofs"


class _MountpointNotMounted(ArtifactValidationError):
    """Internal exact zero-match result for cleanup mountinfo checks."""


# Selected by the PR 2 privileged semantics gate. Runtime activation remains a later PR.
SELECTED_OCI_LAYER_FILESYSTEM = FilesystemCandidate.SQUASHFS


def get_oci_layer_filesystem() -> FilesystemCandidate:
    """Return the immutable Phase 1 choice established by the PR 2 gate."""
    return SELECTED_OCI_LAYER_FILESYSTEM


@dataclass(frozen=True)
class ProbePrerequisites:
    """Resolved commands and host identity checked before probe mutation."""

    candidate: FilesystemCandidate
    packer: str
    packer_version: str
    packer_sha256: str
    mount: str
    umount: str
    kernel_release: str
    architecture: str


@dataclass(frozen=True)
class CandidateProbeEvidence:
    """Successful, candidate-specific filesystem semantics evidence."""

    schema_version: int
    candidate: str
    fixture_digest: str
    kernel_release: str
    architecture: str
    packer_version: str
    packer_sha256: str
    pack_commands: tuple[tuple[str, ...], ...]
    lower_mount_request_options: tuple[str, ...]
    overlay_mount_request_options: tuple[str, ...]
    base_image_sha256: str
    leaf_image_sha256: str
    merged_receipt_sha256: str

    def to_json(self) -> str:
        """Return stable JSON suitable for retained CI evidence."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n"


def check_platform_support() -> None:
    """Ensure host platform supports OCI filesystem probing and mounting.

    Must be called BEFORE executing any packer or mount subprocesses.
    """
    if sys.platform != "linux":
        raise UnsupportedPlatformError(
            f"OCI layer filesystem probing and OverlayFS mounting require a Linux host; {sys.platform} is unsupported."
        )


def _candidate(value: FilesystemCandidate | str) -> FilesystemCandidate:
    try:
        return FilesystemCandidate(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported filesystem candidate: {value}") from exc


def _subprocess_env() -> dict[str, str]:
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
        "TZ": "UTC",
    }


def _run_checked(
    command: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: int = _PACK_TIMEOUT_SECONDS,
    failure_label: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            env=_subprocess_env(),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactValidationError(f"{failure_label} could not complete") from exc
    if result.returncode != 0:
        raise ArtifactValidationError(f"{failure_label} failed with exit code {result.returncode}")
    return result


def to_base36(n: int) -> str:
    """Convert non-negative integer to lowercase base36 representation."""
    if n < 0:
        raise ValueError("base36 index must be non-negative")
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    res: list[str] = []
    while n > 0:
        n, rem = divmod(n, 36)
        res.append(chars[rem])
    return "".join(reversed(res))


def check_lowerdir_page_budget(num_layers: int = 128) -> tuple[bool, int, int]:
    """Check if lowerdir string for num_layers stays below 75% of host page size limit.

    Returns:
        (is_valid, opt_bytes_length, budget_bytes)
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, AttributeError, OSError):
        page_size = 4096

    budget = int(0.75 * page_size)
    paths = [f"/l/{to_base36(i)}" for i in reversed(range(num_layers))]
    lowerdir_opt = "lowerdir=" + ":".join(paths)
    opt_bytes = lowerdir_opt.encode("utf-8")
    length = len(opt_bytes)
    return (length <= budget, length, budget)


def _validate_tar_path(path: str) -> str:
    """Normalize and validate archive path against traversal and reserved paths."""
    if not path or path == ".":
        return "."
    if ".." in path.split("/"):
        raise ArtifactValidationError("Invalid member path in OCI layer tar")
    normalized = os.path.normpath(path)
    if (
        normalized.startswith("/")
        or normalized.startswith("../")
        or "/../" in normalized
        or normalized == ".."
        or "\x00" in path
        or "\\" in path
    ):
        raise ArtifactValidationError("Invalid member path in OCI layer tar")
    if normalized == ".palimpsest" or normalized.startswith(".palimpsest/"):
        raise ArtifactValidationError("Reserved path in OCI layer tar")
    return normalized


def _validate_capability_xattr(value: str) -> bytes:
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ArtifactValidationError("Invalid security.capability metadata") from exc
    if len(raw) not in {12, 20, 24}:
        raise ArtifactValidationError("Invalid security.capability metadata")
    magic = int.from_bytes(raw[:4], "little")
    revision = magic & 0xFF000000
    expected_size = {0x01000000: 12, 0x02000000: 20, 0x03000000: 24}.get(revision)
    if expected_size != len(raw) or magic & 0x00FFFFFE:
        raise ArtifactValidationError("Invalid security.capability metadata")
    return raw


def _validate_pax_xattr_policy(pax_headers: dict[str, str]) -> None:
    for pax_key, pax_value in pax_headers.items():
        if pax_key.startswith("LIBARCHIVE.xattr."):
            raise ArtifactValidationError("OCI layer contains an unsupported LIBARCHIVE xattr encoding")
        if pax_key.startswith("SCHILY.xattr.trusted."):
            raise ArtifactValidationError("OCI layer contains reserved trusted xattr metadata")
        if pax_key.startswith("SCHILY.xattr.security."):
            if pax_key != "SCHILY.xattr.security.capability":
                raise ArtifactValidationError("OCI layer contains unsupported security xattr metadata")
            _validate_capability_xattr(pax_value)
        if pax_key.startswith("SCHILY.xattr.") and not (
            pax_key.startswith("SCHILY.xattr.user.") or pax_key == "SCHILY.xattr.security.capability"
        ):
            raise ArtifactValidationError("OCI layer contains an unsupported xattr namespace")
        if pax_key.startswith("SCHILY.xattr."):
            xattr_name = pax_key.removeprefix("SCHILY.xattr.")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", xattr_name):
                raise ArtifactValidationError("OCI layer contains an invalid xattr name")


def _canonical_output_pax_headers(pax_headers: dict[str, str]) -> dict[str, str]:
    """Retain only reviewed xattr keys; tarfile regenerates canonical structural PAX."""
    return {key: value for key, value in pax_headers.items() if key.startswith("SCHILY.xattr.")}


def _validate_member_numeric_metadata(member: tarfile.TarInfo) -> None:
    """Reject values SquashFS cannot preserve exactly before invoking a packer."""
    for name, value in (("mode", member.mode), ("uid", member.uid), ("gid", member.gid)):
        maximum = 0o7777 if name == "mode" else 0xFFFFFFFF
        if type(value) is not int or not 0 <= value <= maximum:
            raise ArtifactValidationError(f"OCI layer {name} metadata is out of range")
    mtime = member.mtime
    if (
        not isinstance(mtime, (int, float))
        or isinstance(mtime, bool)
        or not math.isfinite(mtime)
        or not float(mtime).is_integer()
        or not 0 <= mtime <= 0xFFFFFFFF
    ):
        raise ArtifactValidationError("OCI layer mtime metadata is not an exactly representable SquashFS timestamp")


def _table_parent(path: str) -> str | None:
    if path == ".":
        return None
    parent = os.path.dirname(path)
    return parent or "."


def _register_table_path(children: dict[str, set[str]], path: str) -> None:
    parent = _table_parent(path)
    if parent is not None:
        children.setdefault(parent, set()).add(path)


def _remove_table_subtree(
    table: dict[str, dict],
    children: dict[str, set[str]],
    path: str,
    *,
    include_root: bool,
) -> None:
    """Remove a path subtree in time proportional to removed descendants."""
    pending = list(children.get(path, ()))
    while pending:
        descendant = pending.pop()
        pending.extend(children.pop(descendant, ()))
        table.pop(descendant, None)
        parent = _table_parent(descendant)
        if parent is not None:
            children.get(parent, set()).discard(descendant)
    children.pop(path, None)
    if include_root:
        table.pop(path, None)
        parent = _table_parent(path)
        if parent is not None:
            children.get(parent, set()).discard(path)


def _validate_hardlink_graph(table: dict[str, dict]) -> None:
    for path, item in table.items():
        if item["is_whiteout"] or not item["member"].islnk():
            continue
        target = item["canonical_linkname"]
        target_item = table.get(target)
        if target_item is None or target_item["is_whiteout"] or not target_item["member"].isreg():
            raise ArtifactValidationError("OCI hardlink target must be a same-layer regular file")
        if target == path:
            raise ArtifactValidationError("OCI hardlink cannot target itself")


def _validate_uncompressed_tar_framing(payload: bytes) -> None:
    """Reject truncated, corrupt, compressed, or nonzero-trailed tar streams."""
    if payload.startswith((b"\x1f\x8b", b"\x28\xb5\x2f\xfd")):
        raise ArtifactValidationError(
            "OCI layer must be decompressed and diff-id verified by registry ingestion before translation"
        )
    block_size = tarfile.BLOCKSIZE
    if len(payload) < block_size * 2 or len(payload) % block_size:
        raise ArtifactValidationError("OCI layer tar framing is invalid")
    offset = 0
    while offset + block_size <= len(payload):
        header = payload[offset : offset + block_size]
        if header == b"\0" * block_size:
            second = payload[offset + block_size : offset + 2 * block_size]
            if second != b"\0" * block_size or any(payload[offset + 2 * block_size :]):
                raise ArtifactValidationError("OCI layer tar trailer is invalid")
            return
        checksum_field = header[148:156].strip(b"\0 ")
        size_field = header[124:136].strip(b"\0 ")
        try:
            stored_checksum = int(checksum_field or b"0", 8)
            member_size = int(size_field or b"0", 8)
        except ValueError as exc:
            raise ArtifactValidationError("OCI layer tar header is invalid") from exc
        checksum_header = header[:148] + (b" " * 8) + header[156:]
        if sum(checksum_header) != stored_checksum:
            raise ArtifactValidationError("OCI layer tar header checksum is invalid")
        data_blocks = (member_size + block_size - 1) // block_size
        offset += block_size * (1 + data_blocks)
        if offset > len(payload):
            raise ArtifactValidationError("OCI layer tar member is truncated")
    raise ArtifactValidationError("OCI layer tar end marker is missing")


def translate_oci_tar_to_overlay_tar(
    in_tar_bytes: bytes,
    *,
    limits: TarTranslationLimits = DEFAULT_TAR_TRANSLATION_LIMITS,
) -> bytes:
    """Translate an already decompressed OCI layer tar into an OverlayFS tar.

    Compression/media-type handling and diff-id verification are deliberately an
    upstream contract for the later registry ingestion stage. Compressed blobs are
    rejected here rather than implicitly decompressed without an integrity check.

    Translates OCI whiteouts:
    - Ordinary whiteout (.wh.NAME) -> character device 0:0 at NAME (mode S_IFCHR | 0600)
    - Opaque whiteout (.wh..wh..opq) -> PAX header SCHILY.xattr.trusted.overlay.opaque=y on parent directory
    - Encodes xattrs into PAX headers SCHILY.xattr.<key>
    - Resolves same-layer whiteout/replacement last-wins semantics before emission.
    - Synthesizes parent directory entries for file-to-directory transitions.
    - Emits hardlink entries after their target regular files.
    """
    if len(in_tar_bytes) > limits.max_input_bytes:
        raise ArtifactValidationError("OCI layer exceeds the input-byte limit")
    _validate_uncompressed_tar_framing(in_tar_bytes)
    in_buf = io.BytesIO(in_tar_bytes)
    opaque_dirs: set[str] = set()
    table: dict[str, dict] = {}
    children: dict[str, set[str]] = {}
    member_count = 0
    total_regular_bytes = 0

    try:
        in_tar_context = tarfile.open(fileobj=in_buf, mode="r:")
    except (tarfile.TarError, OSError) as exc:
        raise ArtifactValidationError("OCI layer is not a readable tar archive") from exc
    with in_tar_context as in_tar:
        while True:
            try:
                member = in_tar.next()
            except (tarfile.TarError, OSError, ValueError, OverflowError) as exc:
                raise ArtifactValidationError("OCI layer contains invalid tar metadata") from exc
            if member is None:
                break
            member_count += 1
            if member_count > limits.max_members:
                raise ArtifactValidationError("OCI layer exceeds the member-count limit")
            try:
                member_name_bytes = member.name.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ArtifactValidationError("OCI layer member path is not valid UTF-8") from exc
            if len(member_name_bytes) > limits.max_path_bytes:
                raise ArtifactValidationError("OCI layer member path exceeds the byte limit")
            norm_name = _validate_tar_path(member.name)
            if not norm_name:
                continue
            basename = os.path.basename(norm_name)
            parent_dir = os.path.dirname(norm_name)
            _validate_member_numeric_metadata(member)
            if basename == ".wh..wh..opq" or basename.startswith(".wh."):
                _validate_whiteout_marker(member)
            if member.isreg():
                if member.sparse:
                    raise ArtifactValidationError("OCI sparse files are not supported by the feasibility probe")
                if member.size < 0 or member.size > limits.max_regular_file_bytes:
                    raise ArtifactValidationError("OCI layer regular file exceeds the per-file limit")
                total_regular_bytes += member.size
                if total_regular_bytes > limits.max_total_regular_bytes:
                    raise ArtifactValidationError("OCI layer exceeds the total regular-byte limit")
            if member.islnk() or member.issym():
                try:
                    link_name_bytes = member.linkname.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ArtifactValidationError("OCI layer link target is not valid UTF-8") from exc
                if len(link_name_bytes) > limits.max_path_bytes:
                    raise ArtifactValidationError("OCI layer link target exceeds the byte limit")
            if member.islnk():
                _validate_tar_path(member.linkname)
            elif member.issym() and (not member.linkname or "\x00" in member.linkname):
                raise ArtifactValidationError("OCI layer symlink target is invalid")
            if not any(
                predicate()
                for predicate in (
                    member.isreg,
                    member.isdir,
                    member.islnk,
                    member.issym,
                    member.ischr,
                    member.isblk,
                    member.isfifo,
                )
            ):
                raise ArtifactValidationError("OCI layer contains an unsupported member type")
            if member.ischr() or member.isblk():
                if not (0 <= member.devmajor <= 4095 and 0 <= member.devminor <= 1_048_575):
                    raise ArtifactValidationError("OCI layer device metadata is out of range")
            try:
                pax_size = sum(
                    len(str(key).encode("utf-8")) + len(str(value).encode("utf-8"))
                    for key, value in (member.pax_headers or {}).items()
                )
            except UnicodeEncodeError as exc:
                raise ArtifactValidationError("OCI layer PAX metadata is not valid UTF-8") from exc
            if pax_size > limits.max_pax_bytes_per_member:
                raise ArtifactValidationError("OCI layer PAX metadata exceeds the per-member limit")
            if norm_name == ".":
                if not member.isdir():
                    raise ArtifactValidationError("OCI root archive member must be a directory")
                root_pax_headers = dict(member.pax_headers or {})
                _validate_pax_xattr_policy(root_pax_headers)
                root_pax_headers = _canonical_output_pax_headers(root_pax_headers)
                table["."] = {
                    "is_whiteout": False,
                    "name": ".",
                    "member": member,
                    "content": None,
                    "pax_headers": root_pax_headers,
                    "canonical_linkname": "",
                }
                _register_table_path(children, ".")
                continue
            if basename == ".wh..wh..opq":
                opaque_dirs.add(parent_dir or ".")
                continue

            if basename.startswith(".wh."):
                target_name = basename[4:]
                if not target_name or target_name in {".", ".."} or target_name.startswith(".wh."):
                    raise ArtifactValidationError("Invalid OCI whiteout target")
                target_path = _validate_tar_path(os.path.join(parent_dir, target_name) if parent_dir else target_name)

                _remove_table_subtree(table, children, target_path, include_root=True)

                table[target_path] = {
                    "is_whiteout": True,
                    "name": target_path,
                    "mtime": member.mtime,
                }
                _register_table_path(children, target_path)
            else:
                # If non-directory member replaces a directory, purge pending descendants
                if not member.isdir():
                    _remove_table_subtree(table, children, norm_name, include_root=False)

                pax_headers = dict(member.pax_headers or {})
                _validate_pax_xattr_policy(pax_headers)
                pax_headers = _canonical_output_pax_headers(pax_headers)
                if member.isreg():
                    extracted = in_tar.extractfile(member)
                    if extracted is None:
                        raise ArtifactValidationError("OCI regular file payload is unavailable")
                    try:
                        content = extracted.read(member.size + 1)
                    except (tarfile.TarError, OSError) as exc:
                        raise ArtifactValidationError("OCI regular file payload could not be read") from exc
                    if len(content) != member.size:
                        raise ArtifactValidationError("OCI regular file payload size does not match metadata")
                else:
                    content = None

                table[norm_name] = {
                    "is_whiteout": False,
                    "name": norm_name,
                    "member": member,
                    "content": content,
                    "pax_headers": pax_headers,
                    "canonical_linkname": _validate_tar_path(member.linkname) if member.islnk() else member.linkname,
                }
                _register_table_path(children, norm_name)

                # Synthesize missing parent directories for file-to-directory transitions
                curr = parent_dir
                while curr and curr != ".":
                    if curr not in table or table[curr].get("is_whiteout") or not table[curr]["member"].isdir():
                        synth_info = tarfile.TarInfo(name=curr)
                        synth_info.type = tarfile.DIRTYPE
                        synth_info.mode = 0o755 | stat.S_IFDIR
                        synth_info.uid = 0
                        synth_info.gid = 0
                        synth_info.mtime = member.mtime
                        table[curr] = {
                            "is_whiteout": False,
                            "name": curr,
                            "member": synth_info,
                            "content": None,
                            "pax_headers": {},
                            "canonical_linkname": "",
                        }
                        _register_table_path(children, curr)
                    curr = os.path.dirname(curr)

    _validate_hardlink_graph(table)

    if "." not in table:
        root_info = tarfile.TarInfo(name=".")
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o755 | stat.S_IFDIR
        root_info.uid = 0
        root_info.gid = 0
        root_info.mtime = 0
        table["."] = {
            "is_whiteout": False,
            "name": ".",
            "member": root_info,
            "content": None,
            "pax_headers": {},
            "canonical_linkname": "",
        }
        _register_table_path(children, ".")

    # Ensure opaque dirs have directory entries in table with opaque PAX header
    for opq_dir in opaque_dirs:
        if opq_dir not in table or table[opq_dir].get("is_whiteout") or not table[opq_dir]["member"].isdir():
            dir_info = tarfile.TarInfo(name=opq_dir)
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755 | stat.S_IFDIR
            dir_info.uid = 0
            dir_info.gid = 0
            dir_info.mtime = 0
            table[opq_dir] = {
                "is_whiteout": False,
                "name": opq_dir,
                "member": dir_info,
                "content": None,
                "pax_headers": {"SCHILY.xattr.trusted.overlay.opaque": "y"},
                "canonical_linkname": "",
            }
            _register_table_path(children, opq_dir)
        else:
            table[opq_dir]["pax_headers"]["SCHILY.xattr.trusted.overlay.opaque"] = "y"

    # Order paths for emission: topological sort for hardlinks
    sorted_paths = sorted(table.keys())
    emitted_set: set[str] = set()
    ordered_paths: list[str] = []

    remaining = list(sorted_paths)
    while remaining:
        progress = False
        next_remaining = []
        for p in remaining:
            item = table[p]
            if not item["is_whiteout"] and item["member"].islnk():
                target = _validate_tar_path(item["member"].linkname)
                if target in table and target not in emitted_set:
                    next_remaining.append(p)
                    continue
            ordered_paths.append(p)
            emitted_set.add(p)
            progress = True
        if not progress:
            ordered_paths.extend(next_remaining)
            break
        remaining = next_remaining

    # Emit output tar
    out_buf = io.BytesIO()
    with tarfile.open(fileobj=out_buf, mode="w", format=tarfile.PAX_FORMAT) as out_tar:
        for p in ordered_paths:
            item = table[p]
            if item["is_whiteout"]:
                wh_info = tarfile.TarInfo(name=p)
                wh_info.type = tarfile.CHRTYPE
                wh_info.mode = 0o600 | stat.S_IFCHR
                wh_info.uid = 0
                wh_info.gid = 0
                wh_info.size = 0
                wh_info.mtime = item["mtime"]
                wh_info.devmajor = 0
                wh_info.devminor = 0
                out_tar.addfile(wh_info)
            else:
                orig_member = item["member"]
                new_info = tarfile.TarInfo(name=p)
                new_info.type = orig_member.type
                new_info.mode = orig_member.mode
                new_info.uid = orig_member.uid
                new_info.gid = orig_member.gid
                new_info.uname = str(orig_member.uid)
                new_info.gname = str(orig_member.gid)
                new_info.size = orig_member.size
                new_info.mtime = orig_member.mtime
                new_info.linkname = item["canonical_linkname"]
                new_info.devmajor = orig_member.devmajor
                new_info.devminor = orig_member.devminor
                pax_headers = dict(item["pax_headers"])
                pax_headers.setdefault("uid", str(orig_member.uid))
                pax_headers.setdefault("gid", str(orig_member.gid))
                if pax_headers:
                    new_info.pax_headers = pax_headers

                if orig_member.isreg() and item["content"] is not None:
                    out_tar.addfile(new_info, io.BytesIO(item["content"]))
                else:
                    out_tar.addfile(new_info)

    return out_buf.getvalue()


def _validate_whiteout_marker(member: tarfile.TarInfo) -> None:
    allowed_structural_pax = {"atime", "ctime", "mtime", "path"}
    if not member.isreg() or member.size != 0 or set(member.pax_headers or {}) - allowed_structural_pax:
        raise ArtifactValidationError("OCI whiteout marker must be an empty regular file without control metadata")


def _squashfs_root_arguments(translated_tar: bytes) -> list[str]:
    """Make mksquashfs preserve the tar root inode it otherwise synthesizes."""
    try:
        with tarfile.open(fileobj=io.BytesIO(translated_tar), mode="r:") as archive:
            root = archive.getmember(".")
    except (KeyError, tarfile.TarError, OSError) as exc:
        raise ArtifactValidationError("Translated OCI layer root metadata is unavailable") from exc
    _validate_member_numeric_metadata(root)
    arguments = [
        "-root-mode",
        f"{root.mode & 0o7777:o}",
        "-root-uid",
        str(root.uid),
        "-root-gid",
        str(root.gid),
        "-root-time",
        str(int(root.mtime)),
    ]
    for key, value in sorted((root.pax_headers or {}).items()):
        if not key.startswith("SCHILY.xattr."):
            continue
        name = key.removeprefix("SCHILY.xattr.")
        try:
            raw_value = value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ArtifactValidationError("OCI root xattr is not byte-preserving Latin-1 metadata") from exc
        encoded = base64.b64encode(raw_value).decode("ascii")
        arguments.extend(("-p", f"/ x {name}=0s{encoded}"))
    return arguments


def _resolve_packer(candidate: FilesystemCandidate, packer_path: Path | None) -> str:
    packer_name = "mksquashfs" if candidate is FilesystemCandidate.SQUASHFS else "mkfs.erofs"
    if packer_path is None:
        resolved = shutil.which(packer_name)
    else:
        resolved = str(packer_path)
        if not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
            raise UnsupportedPlatformError("Explicit filesystem packer is not an executable regular file")
    if not resolved:
        raise UnsupportedPlatformError(f"{packer_name} tool is not installed or not in PATH")
    return resolved


def build_layer_filesystem(
    tar_bytes: bytes,
    *,
    target_fs: FilesystemCandidate | str,
    out_path: Path | None = None,
    packer_path: Path | None = None,
    expected_packer_sha256: str | None = None,
) -> Path:
    """Build one candidate without selection; a generated path owns its private parent."""
    check_platform_support()
    candidate = _candidate(target_fs)

    translated_tar = translate_oci_tar_to_overlay_tar(tar_bytes)

    private_dir: Path | None = None
    publish_path: Path | None = None
    published_identity: tuple[int, int] | None = None
    if out_path is None:
        private_dir = Path(tempfile.mkdtemp(prefix=f"oci_layer_{candidate.value}_"))
        private_dir.chmod(0o700)
        out_path = private_dir / f"layer.{candidate.value}"
    elif os.path.lexists(out_path):
        raise ArtifactValidationError("Candidate output path already exists")

    try:
        parent_stat = out_path.parent.stat()
    except OSError as exc:
        if private_dir is not None:
            private_dir.rmdir()
        raise ArtifactValidationError("Candidate output parent is unavailable") from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & 0o022:
        if private_dir is not None:
            private_dir.rmdir()
        raise ArtifactValidationError(
            "Candidate output parent must be owned by the caller and not group/world writable"
        )

    if private_dir is None:
        publish_path = out_path
        private_dir = Path(tempfile.mkdtemp(prefix=f".{out_path.name}.", dir=out_path.parent))
        private_dir.chmod(0o700)
        out_path = private_dir / f"artifact.{candidate.value}"

    try:
        resolved_packer = _resolve_packer(candidate, packer_path)
    except Exception:
        if private_dir is not None:
            private_dir.rmdir()
        raise
    try:
        if expected_packer_sha256 is not None and _sha256_file(Path(resolved_packer)) != expected_packer_sha256:
            raise ArtifactValidationError("Filesystem packer identity changed before candidate build")
    except Exception:
        if private_dir is not None:
            private_dir.rmdir()
        raise

    try:
        if candidate is FilesystemCandidate.SQUASHFS:
            command = [
                resolved_packer,
                "-",
                str(out_path),
                "-tar",
                "-noappend",
                "-xattrs",
                "-mkfs-time",
                "0",
                "-processors",
                "1",
                *_squashfs_root_arguments(translated_tar),
            ]
            _run_checked(command, input_bytes=translated_tar, failure_label="mksquashfs")
        else:
            tmp_tar_fd, tmp_tar_path = tempfile.mkstemp(prefix="oci_layer_erofs_tar_", suffix=".tar")
            try:
                with os.fdopen(tmp_tar_fd, "wb") as tmp_tar:
                    tmp_tar.write(translated_tar)
                    tmp_tar.flush()
                    os.fsync(tmp_tar.fileno())
                command = [
                    resolved_packer,
                    "--tar=f",
                    "--ovlfs-strip=0",
                    "-T",
                    "0",
                    "-U",
                    _EROFS_UUID,
                    "--preserve-mtime",
                    str(out_path),
                    tmp_tar_path,
                ]
                _run_checked(command, failure_label="mkfs.erofs")
            finally:
                if os.path.exists(tmp_tar_path):
                    os.unlink(tmp_tar_path)

        _verify_candidate_image(candidate, out_path)
        if expected_packer_sha256 is not None and _sha256_file(Path(resolved_packer)) != expected_packer_sha256:
            raise ArtifactValidationError("Filesystem packer identity changed during candidate build")
        with out_path.open("rb") as image_file:
            os.fsync(image_file.fileno())
        if publish_path is not None:
            try:
                os.link(out_path, publish_path)
            except FileExistsError as exc:
                raise ArtifactValidationError("Candidate output path appeared before atomic publish") from exc
            except OSError as exc:
                raise ArtifactValidationError("Candidate output could not be atomically published") from exc
            staged_stat = out_path.stat()
            published_stat = publish_path.lstat()
            if (staged_stat.st_dev, staged_stat.st_ino) != (published_stat.st_dev, published_stat.st_ino):
                raise ArtifactValidationError("Candidate output identity changed during atomic publish")
            published_identity = (published_stat.st_dev, published_stat.st_ino)
            try:
                parent_fd = os.open(publish_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError as exc:
                raise ArtifactValidationError("Candidate output parent could not be durably synchronized") from exc
            out_path.unlink()
            private_dir.rmdir()
            return publish_path
    except Exception:
        if publish_path is not None and published_identity is not None:
            try:
                current = publish_path.lstat()
                if (current.st_dev, current.st_ino) == published_identity:
                    publish_path.unlink()
            except OSError:
                pass
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        if private_dir is not None:
            try:
                private_dir.rmdir()
            except OSError:
                pass
        raise
    return out_path


def _verify_candidate_image(candidate: FilesystemCandidate, image_path: Path) -> None:
    try:
        image_size = image_path.stat().st_size
        with image_path.open("rb") as image_file:
            if candidate is FilesystemCandidate.SQUASHFS:
                magic = image_file.read(4)
                valid_magic = magic in {b"hsqs", b"sqsh"}
            else:
                image_file.seek(1024)
                valid_magic = image_file.read(4) == b"\xe2\xe1\xf5\xe0"
    except OSError as exc:
        raise ArtifactValidationError("Candidate image could not be verified") from exc
    if image_size <= 0 or not valid_magic:
        raise ArtifactValidationError("Candidate image has invalid filesystem magic or size")


def preflight_oci_filesystem_probe(candidate: FilesystemCandidate | str) -> ProbePrerequisites:
    """Fail closed on missing Linux privileges or tools before creating probe state."""
    check_platform_support()
    if os.geteuid() != 0:
        raise UnsupportedPlatformError("OCI layer filesystem probing requires root privileges for mounting")

    resolved_candidate = _candidate(candidate)
    packer_name = "mksquashfs" if resolved_candidate is FilesystemCandidate.SQUASHFS else "mkfs.erofs"
    commands = {name: shutil.which(name) for name in (packer_name, "mount", "umount")}
    missing = [name for name, path in commands.items() if path is None]
    if missing:
        raise UnsupportedPlatformError("OCI filesystem probe prerequisite is missing: " + ", ".join(sorted(missing)))
    if not Path("/proc/self/mountinfo").is_file():
        raise UnsupportedPlatformError("OCI filesystem probe requires Linux mountinfo")

    capabilities = _effective_linux_capabilities()
    required_capabilities = {21}  # CAP_SYS_ADMIN
    if not required_capabilities.issubset(capabilities):
        raise UnsupportedPlatformError("OCI filesystem probe requires effective CAP_SYS_ADMIN")

    is_budget_ok, opt_len, budget = check_lowerdir_page_budget(128)
    if not is_budget_ok:
        raise ArtifactValidationError(
            f"128-lowerdir option string length ({opt_len} bytes) exceeds 75% page-size budget ({budget} bytes)"
        )
    packer_sha256 = _sha256_file(Path(commands[packer_name] or ""))
    packer_version = _read_packer_version(resolved_candidate, commands[packer_name] or "")
    if _sha256_file(Path(commands[packer_name] or "")) != packer_sha256:
        raise ArtifactValidationError("Filesystem packer changed while checking its version")
    return ProbePrerequisites(
        candidate=resolved_candidate,
        packer=commands[packer_name] or "",
        packer_version=packer_version,
        packer_sha256=packer_sha256,
        mount=commands["mount"] or "",
        umount=commands["umount"] or "",
        kernel_release=platform.release(),
        architecture=platform.machine(),
    )


def _effective_linux_capabilities(status_path: Path = Path("/proc/self/status")) -> set[int]:
    try:
        lines = status_path.read_text(encoding="utf-8").splitlines()
        encoded = next(line.split(":", 1)[1].strip() for line in lines if line.startswith("CapEff:"))
        mask = int(encoded, 16)
    except (OSError, StopIteration, ValueError) as exc:
        raise UnsupportedPlatformError("OCI filesystem probe could not read effective Linux capabilities") from exc
    return {bit for bit in range(mask.bit_length()) if mask & (1 << bit)}


def _decode_mountinfo_path(value: str) -> str:
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def _mountinfo_entry(mountpoint: Path, mountinfo_path: Path = Path("/proc/self/mountinfo")) -> tuple[str, set[str]]:
    wanted = str(mountpoint.resolve())
    try:
        lines = mountinfo_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ArtifactValidationError("Failed to read effective mount options") from exc
    matches: list[tuple[str, set[str]]] = []
    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            left_fields = left.split()
            right_fields = right.split()
        except (IndexError, ValueError):
            raise ArtifactValidationError("Mountinfo contains a malformed entry") from None
        if len(left_fields) < 6 or len(right_fields) < 3:
            raise ArtifactValidationError("Mountinfo contains a malformed entry")
        if _decode_mountinfo_path(left_fields[4]) != wanted:
            continue
        options = set(left_fields[5].split(",")) | set(right_fields[2].split(","))
        matches.append((right_fields[0], options))
    if not matches:
        raise _MountpointNotMounted("Mountpoint is not present in mountinfo")
    if len(matches) != 1:
        raise ArtifactValidationError("Mountinfo contains duplicate entries for the mountpoint")
    return matches[0]


def _assert_mount_flags(mountpoint: Path, expected_fs: str, required: set[str]) -> None:
    observed_fs, options = _mountinfo_entry(mountpoint)
    if observed_fs != expected_fs or not required.issubset(options):
        raise ArtifactValidationError("Effective filesystem type or mount flags do not satisfy the probe contract")


def _probe_fixture_digest(base_tar: bytes, leaf_tar: bytes, expected_receipt: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for payload in (
        base_tar,
        leaf_tar,
        json.dumps(expected_receipt, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ):
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_packer_version(candidate: FilesystemCandidate, packer: str) -> str:
    argument = "-version" if candidate is FilesystemCandidate.SQUASHFS else "--help"
    result = _run_checked([packer, argument], failure_label="filesystem packer version")
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if candidate is FilesystemCandidate.EROFS:
        if not all(flag in output for flag in ("--tar=[fi]", "--preserve-mtime", "--ovlfs-strip=[01]")):
            raise UnsupportedPlatformError("mkfs.erofs lacks the required tar and mtime features")
        return "unreported; required flags present"
    pattern = r"mksquashfs version (\d+)\.(\d+)(?:\.(\d+))?"
    match = re.search(pattern, output)
    if match is None:
        raise UnsupportedPlatformError("OCI filesystem packer version could not be identified")
    version = tuple(int(component or 0) for component in match.groups())
    minimum = (4, 6, 0)
    if version < minimum:
        raise UnsupportedPlatformError(
            f"OCI filesystem packer is too old; {candidate.value} requires {'.'.join(map(str, minimum))} or newer"
        )
    return ".".join(map(str, version))


def probe_oci_filesystem_candidate(
    candidate: FilesystemCandidate | str,
    base_tar: bytes,
    leaf_tar: bytes,
    expected_receipt: dict[str, Any],
) -> CandidateProbeEvidence:
    """Prove one candidate independently; never fallback or select a runtime backend."""
    prerequisites = preflight_oci_filesystem_probe(candidate)
    if not hasattr(os, "unshare") or not hasattr(os, "CLONE_NEWNS"):
        raise UnsupportedPlatformError("OCI filesystem probe requires mount namespace isolation")
    try:
        os.unshare(os.CLONE_NEWNS)
    except OSError as exc:
        raise UnsupportedPlatformError("OCI filesystem probe could not create a private mount namespace") from exc
    _run_checked(
        [prerequisites.mount, "--make-rprivate", "/"],
        timeout=_MOUNT_TIMEOUT_SECONDS,
        failure_label="mount namespace isolation",
    )
    fs = prerequisites.candidate.value
    fixture_digest = _probe_fixture_digest(base_tar, leaf_tar, expected_receipt)
    receipt_bytes = json.dumps(expected_receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    pack_commands: list[tuple[str, ...]] = []

    with tempfile.TemporaryDirectory(prefix=f"oci_probe_{fs}_") as tmpdir:
        tmp_path = Path(tmpdir)
        tmp_path.chmod(0o700)
        verified_packer = tmp_path / "verified-packer"
        try:
            shutil.copyfile(prerequisites.packer, verified_packer)
            verified_packer.chmod(0o500)
        except OSError as exc:
            raise ArtifactValidationError("Filesystem packer could not be pinned for the probe") from exc
        if _sha256_file(verified_packer) != prerequisites.packer_sha256:
            raise ArtifactValidationError("Filesystem packer changed while being pinned for the probe")
        pinned_packer_version = _read_packer_version(prerequisites.candidate, str(verified_packer))
        if _sha256_file(verified_packer) != prerequisites.packer_sha256:
            raise ArtifactValidationError("Pinned filesystem packer changed while checking its version")
        images = {name: tmp_path / f"{name}.{fs}" for name in ("base", "leaf", "base-repeat", "leaf-repeat")}
        mountpoints = {name: tmp_path / name for name in ("base-mnt", "leaf-mnt", "overlay-mnt")}
        for mountpoint in mountpoints.values():
            mountpoint.mkdir(mode=0o700)

        for name, payload in (
            ("base", base_tar),
            ("leaf", leaf_tar),
            ("base-repeat", base_tar),
            ("leaf-repeat", leaf_tar),
        ):
            build_layer_filesystem(
                payload,
                target_fs=prerequisites.candidate,
                out_path=images[name],
                packer_path=verified_packer,
                expected_packer_sha256=prerequisites.packer_sha256,
            )

        if _sha256_file(images["base"]) != _sha256_file(images["base-repeat"]):
            raise ArtifactValidationError("Base candidate image rebuild is not deterministic")
        if _sha256_file(images["leaf"]) != _sha256_file(images["leaf-repeat"]):
            raise ArtifactValidationError("Leaf candidate image rebuild is not deterministic")

        attempted_mounts: list[Path] = []
        body_error: Exception | None = None
        try:
            for layer in ("base", "leaf"):
                mountpoint = mountpoints[f"{layer}-mnt"]
                attempted_mounts.append(mountpoint)
                command = [
                    prerequisites.mount,
                    "-t",
                    fs,
                    "-o",
                    "loop,ro,nodev,nosuid,noexec",
                    str(images[layer]),
                    str(mountpoint),
                ]
                _run_checked(command, timeout=_MOUNT_TIMEOUT_SECONDS, failure_label=f"{fs} lower mount")
                _assert_mount_flags(mountpoint, fs, {"ro", "nodev", "nosuid", "noexec"})

            overlay_options = f"nodev,nosuid,noexec,lowerdir={mountpoints['leaf-mnt']}:{mountpoints['base-mnt']}"
            overlay_command = [
                prerequisites.mount,
                "-t",
                "overlay",
                "overlay",
                "-o",
                overlay_options,
                str(mountpoints["overlay-mnt"]),
            ]
            attempted_mounts.append(mountpoints["overlay-mnt"])
            _run_checked(overlay_command, timeout=_MOUNT_TIMEOUT_SECONDS, failure_label="overlay mount")
            _assert_mount_flags(mountpoints["overlay-mnt"], "overlay", {"nodev", "nosuid", "noexec"})

            _assert_lower_layer_controls(mountpoints["leaf-mnt"])
            receipt_ok, diff_msg = _verify_merged_tree(mountpoints["overlay-mnt"], expected_receipt.get("entries", {}))
            if not receipt_ok:
                raise ArtifactValidationError(f"Merged OCI receipt mismatch: {diff_msg}")
            _assert_device_nodes_inert(mountpoints["base-mnt"], mountpoints["overlay-mnt"])
        except Exception as exc:
            body_error = exc
        cleanup_errors: list[Exception] = []
        for mountpoint in reversed(attempted_mounts):
            try:
                _mountinfo_entry(mountpoint)
            except _MountpointNotMounted:
                continue
            except ArtifactValidationError as exc:
                cleanup_errors.append(exc)
                continue
            try:
                _run_checked(
                    [prerequisites.umount, str(mountpoint)],
                    timeout=_MOUNT_TIMEOUT_SECONDS,
                    failure_label="probe unmount",
                )
            except Exception as exc:
                cleanup_errors.append(exc)
        if body_error is not None and cleanup_errors:
            raise ExceptionGroup("OCI filesystem probe and cleanup both failed", [body_error, *cleanup_errors])
        if body_error is not None:
            raise body_error
        if cleanup_errors:
            raise ExceptionGroup("OCI filesystem probe cleanup failed", cleanup_errors)
        if _sha256_file(verified_packer) != prerequisites.packer_sha256:
            raise ArtifactValidationError("Filesystem packer changed during the probe")

        for payload in (base_tar, leaf_tar, base_tar, leaf_tar):
            if prerequisites.candidate is FilesystemCandidate.SQUASHFS:
                command_template = (
                    "<verified-packer>",
                    "-",
                    "<output>",
                    "-tar",
                    "-noappend",
                    "-xattrs",
                    "-mkfs-time",
                    "0",
                    "-processors",
                    "1",
                    *_squashfs_root_arguments(translate_oci_tar_to_overlay_tar(payload)),
                )
            else:
                command_template = (
                    "<verified-packer>",
                    "--tar=f",
                    "--ovlfs-strip=0",
                    "-T",
                    "0",
                    "-U",
                    _EROFS_UUID,
                    "--preserve-mtime",
                    "<output>",
                    "<translated-tar>",
                )
            pack_commands.append(command_template)
        return CandidateProbeEvidence(
            schema_version=1,
            candidate=fs,
            fixture_digest=fixture_digest,
            kernel_release=prerequisites.kernel_release,
            architecture=prerequisites.architecture,
            packer_version=pinned_packer_version,
            packer_sha256=prerequisites.packer_sha256,
            pack_commands=tuple(pack_commands),
            lower_mount_request_options=("loop", "nodev", "noexec", "nosuid", "ro"),
            overlay_mount_request_options=("nodev", "noexec", "nosuid", "lowerdir=<leaf>:<base>"),
            base_image_sha256=_sha256_file(images["base"]),
            leaf_image_sha256=_sha256_file(images["leaf"]),
            merged_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        )


def _assert_lower_layer_controls(leaf_mnt: Path) -> None:
    whiteout = leaf_mnt / "dir1" / "file2.txt"
    try:
        whiteout_stat = whiteout.lstat()
    except OSError as exc:
        raise ArtifactValidationError("Translated ordinary whiteout is missing from the leaf layer") from exc
    if (
        not stat.S_ISCHR(whiteout_stat.st_mode)
        or os.major(whiteout_stat.st_rdev) != 0
        or os.minor(whiteout_stat.st_rdev) != 0
    ):
        raise ArtifactValidationError("Translated ordinary whiteout metadata is invalid")
    try:
        opaque = os.getxattr(leaf_mnt / "dir_opaque", "trusted.overlay.opaque", follow_symlinks=False)
    except OSError as exc:
        raise ArtifactValidationError("Translated opaque-directory marker is missing from the leaf layer") from exc
    if opaque != b"y":
        raise ArtifactValidationError("Translated opaque-directory marker is invalid")


def _assert_device_nodes_inert(base_mnt: Path, overlay_mnt: Path) -> None:
    """Require a known-openable device node to be denied on lower and final mounts."""
    for root in (base_mnt, overlay_mnt):
        device = root / "char_dev"
        try:
            device_stat = device.lstat()
        except OSError as exc:
            raise ArtifactValidationError("Crafted character device is missing from probe tree") from exc
        if (
            not stat.S_ISCHR(device_stat.st_mode)
            or os.major(device_stat.st_rdev) != 1
            or os.minor(device_stat.st_rdev) != 3
        ):
            raise ArtifactValidationError("Crafted character device metadata is unsafe for the open-denial probe")
        try:
            descriptor = os.open(
                device,
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except PermissionError:
            continue
        except OSError as exc:
            if exc.errno in {1, 13}:  # EPERM or EACCES
                continue
            raise ArtifactValidationError("Crafted character device denial was not caused by nodev") from exc
        else:
            os.close(descriptor)
            raise ArtifactValidationError("Crafted character device opened despite nodev")


def _verify_merged_tree(mnt: Path, expected_entries: dict) -> tuple[bool, str]:
    """Verify mounted OverlayFS directory tree matches expected receipt entries."""
    receipt_error = _validate_expected_entries(expected_entries)
    if receipt_error is not None:
        return (False, receipt_error)
    observed: set[str] = {"."}

    for root, dirs, files in os.walk(mnt):
        rel_root = Path(root).relative_to(mnt)

        for d in dirs:
            rel_path = str(rel_root / d) if str(rel_root) != "." else d
            observed.add(rel_path)

        for f in files:
            rel_path = str(rel_root / f) if str(rel_root) != "." else f
            observed.add(rel_path)

    expected_keys = set(expected_entries.keys())

    # Check for whiteouts: entries in expected_entries must exist; entries omitted must NOT exist
    missing = expected_keys - observed
    if missing:
        return (False, f"Missing expected entries in merged tree: {sorted(missing)}")

    unexpected = observed - expected_keys
    if unexpected:
        return (
            False,
            f"Unexpected/unhidden entries in merged tree (whiteout failure): {sorted(unexpected)}",
        )

    seen_inode_groups: dict[str, tuple[int, int]] = {}
    inode_group_sizes: dict[str, int] = {}
    for expected in expected_entries.values():
        if "inode_group" in expected:
            group = str(expected["inode_group"])
            inode_group_sizes[group] = inode_group_sizes.get(group, 0) + 1

    for rel_path, exp in expected_entries.items():
        p = mnt / rel_path

        if not os.path.lexists(p):
            return (False, f"Path {rel_path} does not exist in merged tree")

        try:
            st = p.lstat()
        except OSError as e:
            return (False, f"Failed to lstat {rel_path}: {e}")

        exp_type = exp.get("type")

        if exp_type == "file" and not stat.S_ISREG(st.st_mode):
            return (False, f"{rel_path} is not regular file")
        elif exp_type == "dir" and not stat.S_ISDIR(st.st_mode):
            return (False, f"{rel_path} is not directory")
        elif exp_type == "symlink" and not stat.S_ISLNK(st.st_mode):
            return (False, f"{rel_path} is not symlink")
        elif exp_type == "fifo" and not stat.S_ISFIFO(st.st_mode):
            return (False, f"{rel_path} is not FIFO")
        elif exp_type == "chardev" and not stat.S_ISCHR(st.st_mode):
            return (False, f"{rel_path} is not char device")
        elif exp_type == "blkdev" and not stat.S_ISBLK(st.st_mode):
            return (False, f"{rel_path} is not block device")

        if "mode" in exp:
            exp_mode_bits = exp["mode"] & 0o7777
            act_mode_bits = st.st_mode & 0o7777
            if act_mode_bits != exp_mode_bits:
                return (
                    False,
                    f"{rel_path} mode mismatch: expected {oct(exp_mode_bits)}, got {oct(act_mode_bits)}",
                )

        if "uid" in exp and st.st_uid != exp["uid"]:
            return (False, f"{rel_path} uid mismatch: expected {exp['uid']}, got {st.st_uid}")

        if "gid" in exp and st.st_gid != exp["gid"]:
            return (False, f"{rel_path} gid mismatch: expected {exp['gid']}, got {st.st_gid}")

        if "mtime_ns" in exp:
            exp_mtime_ns = int(exp["mtime_ns"])
            if st.st_mtime_ns != exp_mtime_ns:
                return (
                    False,
                    f"{rel_path} mtime mismatch: expected {exp_mtime_ns}, got {st.st_mtime_ns}",
                )

        if exp_type in ("chardev", "blkdev"):
            if "rdev_major" in exp:
                act_major = os.major(st.st_rdev)
                if act_major != exp["rdev_major"]:
                    return (
                        False,
                        f"{rel_path} major device number mismatch: expected {exp['rdev_major']}, got {act_major}",
                    )
            if "rdev_minor" in exp:
                act_minor = os.minor(st.st_rdev)
                if act_minor != exp["rdev_minor"]:
                    return (
                        False,
                        f"{rel_path} minor device number mismatch: expected {exp['rdev_minor']}, got {act_minor}",
                    )

        if exp_type == "symlink" and "target" in exp:
            try:
                target = os.readlink(p)
            except OSError as e:
                return (False, f"Failed to readlink {rel_path}: {e}")
            if target != exp["target"]:
                return (
                    False,
                    f"{rel_path} symlink target mismatch: expected {exp['target']}, got {target}",
                )

        if exp_type == "file" and "sha256" in exp:
            try:
                actual_hash = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError as e:
                return (False, f"Failed to read {rel_path} for sha256 check: {e}")
            if actual_hash != exp["sha256"]:
                return (
                    False,
                    f"{rel_path} content hash mismatch: expected {exp['sha256']}, got {actual_hash}",
                )

        link_target = exp.get("link_target") or exp.get("hardlink_target")
        if link_target:
            target_p = mnt / link_target
            if not os.path.lexists(target_p):
                return (False, f"Hardlink target {link_target} for {rel_path} does not exist")
            try:
                target_st = target_p.lstat()
            except OSError as e:
                return (False, f"Failed to lstat hardlink target {link_target} for {rel_path}: {e}")
            if st.st_ino != target_st.st_ino:
                return (
                    False,
                    f"{rel_path} hardlink inode mismatch with {link_target}: {st.st_ino} != {target_st.st_ino}",
                )

        if "inode_group" in exp:
            group = str(exp["inode_group"])
            if group in seen_inode_groups:
                if (st.st_dev, st.st_ino) != seen_inode_groups[group]:
                    return (
                        False,
                        f"{rel_path} inode group {group} identity mismatch",
                    )
            else:
                seen_inode_groups[group] = (st.st_dev, st.st_ino)
            if st.st_nlink != inode_group_sizes[group]:
                return (
                    False,
                    f"{rel_path} inode group {group} link-count mismatch",
                )

        if "xattrs_b64" in exp:
            expected_xattrs = exp["xattrs_b64"]
            if not (hasattr(os, "getxattr") and hasattr(os, "listxattr")):
                return (False, f"Platform does not support xattr verification for {rel_path}")
            try:
                actual_names = set(os.listxattr(p, follow_symlinks=False))
            except OSError as e:
                return (False, f"{rel_path} xattr list error: {e}")
            if actual_names != set(expected_xattrs):
                return (False, f"{rel_path} xattr name set mismatch")
            for attr_name, exp_value_b64 in expected_xattrs.items():
                try:
                    act_val_bytes = os.getxattr(p, attr_name, follow_symlinks=False)
                except OSError as e:
                    return (False, f"{rel_path} xattr {attr_name} read error: {e}")
                try:
                    exp_val_bytes = base64.b64decode(exp_value_b64, validate=True)
                except (ValueError, TypeError) as e:
                    return (False, f"{rel_path} xattr {attr_name} oracle is invalid: {e}")
                if act_val_bytes != exp_val_bytes:
                    return (
                        False,
                        f"{rel_path} xattr {attr_name} mismatch: expected {exp_val_bytes!r}, got {act_val_bytes!r}",
                    )
    return (True, "OK")


def _validate_expected_entries(expected_entries: Any) -> str | None:
    if not isinstance(expected_entries, dict):
        return "Merged receipt entries must be an object"
    common_keys = {"type", "mode", "uid", "gid", "mtime_ns", "xattrs_b64"}
    common_required = common_keys
    type_keys = {
        "file": {"sha256", "link_target", "hardlink_target", "inode_group"},
        "dir": set(),
        "symlink": {"target"},
        "fifo": set(),
        "chardev": {"rdev_major", "rdev_minor"},
        "blkdev": {"rdev_major", "rdev_minor"},
    }
    for rel_path, entry in expected_entries.items():
        if not isinstance(rel_path, str):
            return "Merged receipt path must be a string"
        if rel_path != ".":
            try:
                if _validate_tar_path(rel_path) != rel_path:
                    return "Merged receipt path is not canonical"
            except ArtifactValidationError:
                return "Merged receipt path is unsafe"
        if not isinstance(entry, dict):
            return "Merged receipt entry must be an object"
        entry_type = entry.get("type")
        if not isinstance(entry_type, str) or entry_type not in type_keys:
            return "Merged receipt entry type is missing or invalid"
        required = set(common_required)
        if entry_type == "file":
            required.add("sha256")
        elif entry_type == "symlink":
            required.add("target")
        elif entry_type in {"chardev", "blkdev"}:
            required.update({"rdev_major", "rdev_minor"})
        if not required.issubset(entry):
            return "Merged receipt entry is missing required metadata"
        if set(entry) - common_keys - type_keys[entry_type]:
            return "Merged receipt entry contains unknown fields"
        for numeric_key in ("mode", "uid", "gid", "mtime_ns", "rdev_major", "rdev_minor"):
            if numeric_key in entry and (type(entry[numeric_key]) is not int or entry[numeric_key] < 0):
                return "Merged receipt numeric metadata is invalid"
        if entry_type == "file" and (
            not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            return "Merged receipt file digest is invalid"
        link_fields = [name for name in ("link_target", "hardlink_target") if name in entry]
        if len(link_fields) > 1:
            return "Merged receipt hardlink target metadata is ambiguous"
        for link_field in link_fields:
            link_target = entry[link_field]
            if not isinstance(link_target, str):
                return "Merged receipt hardlink target metadata is invalid"
            try:
                if link_target == "." or _validate_tar_path(link_target) != link_target:
                    return "Merged receipt hardlink target metadata is invalid"
            except ArtifactValidationError:
                return "Merged receipt hardlink target metadata is invalid"
        if "inode_group" in entry and not isinstance(entry["inode_group"], str):
            return "Merged receipt inode group metadata is invalid"
        if entry_type == "symlink" and not isinstance(entry["target"], str):
            return "Merged receipt symlink target is invalid"
        xattrs = entry.get("xattrs_b64")
        if xattrs is not None:
            if not isinstance(xattrs, dict) or not all(
                isinstance(name, str) and isinstance(value, str) for name, value in xattrs.items()
            ):
                return "Merged receipt xattr metadata is invalid"
            try:
                for encoded in xattrs.values():
                    base64.b64decode(encoded, validate=True)
            except ValueError:
                return "Merged receipt xattr metadata is invalid"
    if "." not in expected_entries:
        return "Merged receipt root entry is missing"
    if expected_entries["."].get("type") != "dir":
        return "Merged receipt root entry must be a directory"
    return None
