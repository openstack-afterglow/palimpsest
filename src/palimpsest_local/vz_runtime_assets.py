"""Offline, bounded ARM64 boot assets for the native Lima VZ root profile.

Inputs are public boot components, never a cloud initrd or workload filesystem.
``modules_dir`` is the installed, exact kernel-release directory. Its ``config``
(or ``config-<release>`` beside the kernel) must be the original Ubuntu config.
Optional ``<component>.provenance.json`` sidecars record available build evidence:
source URLs/digests, recipe, toolchain and licenses. These are declarations, not
attestations. Neither builder nor loader executes supplied binaries or tools.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import lzma
import os
import re
import stat
import struct
import sys
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from .errors import ArtifactValidationError, UnsupportedPlatformError
from .oci_provenance import canonical_json_bytes

SCHEMA = "palimpsest.vz-runtime-assets.v1"
PLATFORM_ABI = "palimpsest.vz-platform.v2"
MAX_KERNEL_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 4096
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_PATH = re.compile(r"[A-Za-z0-9_+.,@=-]+(?:/[A-Za-z0-9_+.,@=-]+)*\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MODULE_NAME = re.compile(r"[A-Za-z0-9_]{1,128}\Z")
_MODULE_INDEXES = ("modules.dep", "modules.alias", "modules.softdep", "modules.builtin", "modules.builtin.modinfo")
_OUTPUT = {"Image", "initramfs.cpio.gz", "manifest.json"}
# Qualified ARM64 static Dropbear: pubkey-only; password/PAM/plugin/syslog disabled.
_DROPBEAR_PROFILE = ("sha256:7ecd073d57f4fb68bcbbbab1f75ea4a1b4cd860318df9b3508d95f4515c0b866", 506576)
_COMPONENTS = {
    "platform": "platform/plm-vz",
    "busybox": "platform/busybox",
    "dropbear": "platform/dropbearmulti",
    "guestagent": "platform/lima-guestagent",
}
_FEATURES = {
    "CONFIG_VIRTIO_FS": "virtiofs",
    "CONFIG_OVERLAY_FS": "overlay",
    "CONFIG_VSOCKETS": "vsock",
    "CONFIG_VIRTIO_VSOCKETS": "vmw_vsock_virtio_transport",
    "CONFIG_VIRTIO_NET": "virtio_net",
    "CONFIG_VIRTIO_BLK": "virtio_blk",
    "CONFIG_HW_RANDOM": "rng_core",
    "CONFIG_HW_RANDOM_VIRTIO": "virtio_rng",
    "CONFIG_EXT4_FS": "ext4",
    "CONFIG_SQUASHFS": "squashfs",
    "CONFIG_BLK_DEV_LOOP": "loop",
    "CONFIG_ISO9660_FS": "isofs",
    "CONFIG_INET_DIAG": "inet_diag",
    "CONFIG_INET_TCP_DIAG": "tcp_diag",
    "CONFIG_INET_UDP_DIAG": "udp_diag",
}
_BUILTIN = (
    "CONFIG_ARM64",
    "CONFIG_64BIT",
    "CONFIG_BLK_DEV_INITRD",
    "CONFIG_RD_GZIP",
    "CONFIG_NET",
    "CONFIG_INET",
    "CONFIG_PACKET",
    "CONFIG_UNIX",
    "CONFIG_PROC_FS",
    "CONFIG_SYSFS",
    "CONFIG_TMPFS",
    "CONFIG_DEVTMPFS",
    "CONFIG_BINFMT_ELF",
    "CONFIG_BINFMT_SCRIPT",
    "CONFIG_TTY",
    "CONFIG_UNIX98_PTYS",
    "CONFIG_CGROUPS",
    "CONFIG_MEMCG",
    "CONFIG_CGROUP_PIDS",
    "CONFIG_PROC_CHILDREN",
    "CONFIG_SECURITY_YAMA",
)


@dataclass(frozen=True, slots=True)
class VZRuntimeAssets:
    root: Path
    kernel: Path
    initramfs: Path
    manifest_digest: str
    kernel_release: str
    lima_version: str
    root_device: int
    root_inode: int


def _fail(message: str) -> NoReturn:
    raise ArtifactValidationError("VZ runtime assets: " + message)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _path(name: Any) -> str:
    if (
        type(name) is not str
        or len(name) > 1024
        or _PATH.fullmatch(name) is None
        or any(part in {".", ".."} for part in name.split("/"))
    ):
        _fail("unsafe archive or module path")
    return name


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def _directory(path: Path, *, private: bool = False) -> Iterator[int]:
    """Open every ancestor without following symlinks; retain the final inode."""
    path = Path(os.path.abspath(path.expanduser()))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in {0, os.geteuid()} or (
                info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            ):
                _fail("directory ancestry is not owner-controlled")
        info = os.fstat(fd)
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o500):
            _fail("asset directory must be owned by the caller and mode 0500")
        yield fd
        if (os.stat(path, follow_symlinks=False).st_dev, os.stat(path, follow_symlinks=False).st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            _fail("asset directory changed during use")
    finally:
        os.close(fd)


def _read_at(fd: int, name: str, limit: int, *, immutable: bool = False) -> bytes:
    _path(name)
    parts = name.split("/")
    current = os.dup(fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
                _fail("input directory is not owner-controlled")
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=current)
        try:
            before = os.fstat(source)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid not in {0, os.geteuid()}
                or before.st_mode & 0o022
                or not 0 <= before.st_size <= limit
            ):
                _fail("input must be a bounded, owned, single-link regular file")
            if immutable and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o400):
                _fail("published asset file must be owned by the caller and mode 0400")
            result = bytearray()
            while len(result) <= limit:
                chunk = os.read(source, min(1024 * 1024, limit + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
            if len(result) > limit or len(result) != before.st_size or _identity(before) != _identity(os.fstat(source)):
                _fail("input changed or exceeded its bound while reading")
            after = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            if _identity(before) != _identity(after):
                _fail("input pathname was replaced while reading")
            return bytes(result)
        finally:
            os.close(source)
    finally:
        os.close(current)


def _read(path: Path, limit: int) -> bytes:
    path = Path(os.path.abspath(path.expanduser()))
    with _directory(path.parent) as fd:
        return _read_at(fd, path.name, limit)


def _json(payload: bytes, *, canonical: bool = True) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail("duplicate JSON field")
            result[key] = value
        return result

    try:
        result = json.loads(
            payload, object_pairs_hook=pairs, parse_constant=lambda value: _fail("nonfinite JSON value")
        )
    except (ValueError, UnicodeError, RecursionError):
        _fail("invalid bounded JSON metadata")
    if canonical and canonical_json_bytes(result) != payload:
        _fail("metadata must use canonical sorted compact UTF-8 JSON without a newline")
    return result


def _gunzip(payload: bytes, limit: int) -> bytes:
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        result = decoder.decompress(payload, limit + 1)
        if len(result) > limit or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            _fail("gzip must be one complete bounded stream")
        return result
    except zlib.error:
        _fail("invalid gzip data")


def _image(payload: bytes, release: str) -> str:
    """Validate the documented 64-byte little-endian ARM64 Image boot header."""
    if not 64 <= len(payload) <= MAX_KERNEL_BYTES:
        _fail("kernel Image size is unsupported")
    code0, code1, text_offset, image_size, flags, r2, r3, r4, magic, pe = struct.unpack_from("<IIQQQQQQII", payload)
    # EFI's MZ-compatible signature instruction changed from ADD to CCMP in
    # modern ARM64 kernels; both are documented no-ops before the branch.
    entry_valid = (code0 in {0x91005A4D, 0xFA405A4D} and code1 & 0xFC000000 == 0x14000000) or (
        code0 & 0xFC000000 == 0x14000000 and code1 == 0
    )
    if (
        magic != 0x644D5241
        or not entry_valid
        or not 64 <= image_size <= MAX_KERNEL_BYTES
        or text_offset % 4096
        or flags & 1
        or flags & ~0xF
        or r2
        or r3
        or r4
        or (pe and not 64 <= pe <= len(payload) - 6)
    ):
        _fail("kernel is not a supported uncompressed little-endian ARM64 Linux Image")
    if pe and (payload[pe : pe + 4] != b"PE\0\0" or payload[pe + 4 : pe + 6] != b"\x64\xaa"):
        _fail("kernel EFI header does not identify ARM64")
    banners = re.findall(rb"Linux version ([0-9][^\x00\n]{1,1023})", payload)
    # A real Ubuntu Image can retain both the weak untimestamped banner and
    # the final timestamped banner. Multiplicity is not a release mismatch.
    if not banners or any(
        not banner.startswith(release.encode() + b" ") or b"Ubuntu" not in banner for banner in banners
    ):
        _fail("kernel must have consistent original matching Ubuntu release banners")
    return banners[0].decode("ascii", errors="replace")


def _sections(payload: bytes, *, module: bool = False) -> tuple[tuple[Any, ...], dict[str, bytes]]:
    if not 64 <= len(payload) <= MAX_FILE_BYTES:
        _fail("ELF size is unsupported")
    fields = struct.unpack_from("<16sHHIQQQIHHHHHH", payload)
    ident, kind, machine, version, _entry, _phoff, shoff, flags, ehsize, _phsize, _phnum, shsize, shnum, names = fields
    if (
        ident[:7] != b"\x7fELF\x02\x01\x01"
        or ident[7] not in {0, 3}
        or ident[8:] != b"\0" * 8
        or machine != 183
        or version != 1
        or flags
        or ehsize != 64
        or kind != (1 if module else 2)
    ):
        _fail("expected a little-endian ARM64 " + ("module" if module else "static executable"))
    sections: dict[str, bytes] = {}
    if shnum:
        if shsize != 64 or not 0 < names < shnum or shnum > 8192 or shoff < 64 or shoff + shsize * shnum > len(payload):
            _fail("ELF section table is invalid")
        headers = [struct.unpack_from("<IIQQQQIIQQ", payload, shoff + index * shsize) for index in range(shnum)]
        string_header = headers[names]
        if string_header[1] != 3 or string_header[4] + string_header[5] > len(payload):
            _fail("ELF section-name string table is invalid")
        strings = payload[string_header[4] : string_header[4] + string_header[5]]
        for header in headers:
            name_offset, section_type, _flags, _addr, offset, size, *_rest = header
            if section_type == 8:
                continue
            if offset + size > len(payload) or name_offset >= len(strings):
                _fail("ELF section bounds are invalid")
            end = strings.find(b"\0", name_offset)
            if end < 0:
                _fail("ELF section name is unterminated")
            name = strings[name_offset:end].decode("ascii", errors="strict")
            if name and name in sections:
                _fail("duplicate ELF section name")
            sections[name] = payload[offset : offset + size]
    elif shoff or shsize or names:
        _fail("ELF stripped section table is inconsistent")
    return fields, sections


def _executable(payload: bytes, role: str, lima_version: str) -> dict[str, Any]:
    fields, sections = _sections(payload)
    _ident, _kind, _machine, _version, entry, phoff, _shoff, _flags, _ehsize, phsize, phnum, *_rest = fields
    if phsize != 56 or not 1 <= phnum <= 128 or phoff < 64 or phoff + phsize * phnum > len(payload):
        _fail(role + " has an invalid ELF program table")
    executable_entry = False
    stack = False
    for index in range(phnum):
        kind, flags, offset, virtual, _physical, size, memory, alignment = struct.unpack_from(
            "<IIQQQQQQ", payload, phoff + index * phsize
        )
        if (
            kind in {2, 3}
            or flags & ~7
            or size > memory
            or memory > MAX_ARCHIVE_BYTES
            or offset + size > len(payload)
            or (alignment not in {0, 1} and (alignment & (alignment - 1) or offset % alignment != virtual % alignment))
        ):
            _fail(role + " has dynamic dependencies or invalid ELF segments")
        if kind == 1:
            if flags & 3 == 3:
                _fail(role + " has a writable executable segment")
            executable_entry |= flags & 5 == 5 and virtual <= entry < virtual + size
        if kind == 0x6474E551:
            if stack or flags & 1 or size or memory:
                _fail(role + " has an unsafe ELF stack")
            stack = True
    # Go static binaries may omit PT_GNU_STACK entirely; Linux defaults ARM64 to NX.
    if not executable_entry:
        _fail(role + " does not have a file-backed executable entry")
    markers = {
        "platform": (PLATFORM_ABI.encode(),),
        "busybox": (b"BusyBox v1.36.1",),
        "dropbear": (b"2026.94", b"dropbear", b"dropbearkey"),
        "guestagent": (b"github.com/lima-vm/lima", lima_version.encode()),
    }
    if any(marker not in payload for marker in markers[role]):
        _fail(role + " is missing the selected component version/ABI identity")
    if role == "busybox":
        applets = (
            "modprobe udhcpc ip sh cat uname env printf test true false id readlink mkdir chmod "
            "sleep date touch kill ps wc cut tr sort tee rm ln mv cp stat find timeout "
            "grep sed awk head tail ls"
        ).split()
        if any(b"\0" + applet.encode() + b"\0" not in payload for applet in applets):
            _fail("BusyBox lacks a required native-platform/Lima control applet")
    digest = _digest(payload)
    if role == "dropbear" and (digest, len(payload)) != _DROPBEAR_PROFILE:
        _fail("dropbear does not match the qualified pubkey-only static build")
    comments = sections.get(".comment", b"")[:16384]
    return {
        "digest": digest,
        "size": len(payload),
        "machine": 183,
        "linkage": "static",
        "elf_comment": [part.decode("ascii", errors="replace") for part in comments.split(b"\0") if part],
    }


def _config(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeError:
        _fail("kernel config must be ASCII")
    config: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_"):
            key, sep, value = line.partition("=")
            if not sep or key in config:
                _fail("kernel config contains an invalid or repeated setting")
            config[key] = value
    if any(config.get(name) != "y" for name in _BUILTIN):
        _fail("kernel lacks built-in ARM64/initramfs/network/pseudo-filesystem support")
    for name in _FEATURES:
        if config.get(name) not in {"y", "m"}:
            _fail("kernel lacks required " + name)
    if not any(config.get(name) in {"y", "m"} for name in ("CONFIG_VIRTIO_PCI", "CONFIG_VIRTIO_MMIO")):
        _fail("kernel lacks a supported virtio transport")
    if config.get("CONFIG_SQUASHFS_XZ") != "y" and config.get("CONFIG_SQUASHFS_ZSTD") != "y":
        _fail("kernel lacks xz/zstd SquashFS decompression")
    if any(
        config.get(name) != "y"
        for name in ("CONFIG_SQUASHFS_XATTR", "CONFIG_EXT4_FS_POSIX_ACL", "CONFIG_EXT4_FS_SECURITY")
    ):
        _fail("kernel lacks required SquashFS/ext4 root xattr and ACL preservation")
    return config


def _kernel_config_binding(image: bytes, config_bytes: bytes) -> str:
    start = image.find(b"IKCFG_ST")
    if start < 0:
        return "release-banner-and-module-vermagic-no-embedded-config"
    end = image.find(b"IKCFG_ED", start + 8)
    if end < 0:
        _fail("kernel has an incomplete embedded config")
    embedded = _gunzip(image[start + 8 : end], MAX_METADATA_BYTES)
    if _config(embedded) != _config(config_bytes):
        _fail("original supplied config differs from embedded kernel config")
    return "embedded-ikconfig-and-release-banner-and-module-vermagic"


def _module(payload: bytes, release: str) -> dict[str, Any]:
    _fields, sections = _sections(payload, module=True)
    info: dict[str, list[str]] = {}
    for item in sections.get(".modinfo", b"").split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            info.setdefault(key.decode("ascii"), []).append(value.decode("ascii"))
    vermagic = info.get("vermagic", [])
    if len(vermagic) != 1 or not vermagic[0].startswith(release + " ") or not info.get("license"):
        _fail("kernel module has missing/mismatched vermagic or license")
    return {
        "digest": _digest(payload),
        "size": len(payload),
        "vermagic": vermagic[0],
        "license": info["license"],
        "srcversion": info.get("srcversion", []),
        "depends": info.get("depends", [""]),
    }


class _ZstdDecoder:
    """Linux build-time only: pin the existing native library, never ship it."""

    def __init__(self) -> None:
        import ctypes

        if sys.platform != "linux":
            _fail("zstd module decoding is supported only on the offline Linux builder")
        try:
            # Do not let LD_LIBRARY_PATH select code before checking its custody.
            machine = os.uname().machine
            triplet = {"aarch64": "aarch64-linux-gnu", "x86_64": "x86_64-linux-gnu"}.get(machine)
            candidates = [
                Path(prefix) / suffix / "libzstd.so.1"
                for prefix in ("/usr/lib", "/lib")
                for suffix in ([triplet, ""] if triplet else [""])
            ] + [Path("/usr/lib64/libzstd.so.1"), Path("/lib64/libzstd.so.1")]
            library_path = next(
                (Path(os.path.realpath(candidate)) for candidate in candidates if candidate.exists()), None
            )
            if library_path is None:
                _fail("existing system libzstd.so.1 is required")
            if any(parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022 for parent in library_path.parents):
                _fail("libzstd ancestry must be root-owned and nonwritable")
            with _directory(library_path.parent) as directory:
                source = os.open(
                    library_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
                )
                try:
                    before = os.fstat(source)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_uid != 0
                        or before.st_nlink != 1
                        or before.st_mode & 0o022
                        or not 0 < before.st_size <= MAX_FILE_BYTES
                    ):
                        _fail("libzstd must be a bounded root-owned single-link system library")
                    library_bytes = _read_at(directory, library_path.name, MAX_FILE_BYTES)
                    if _identity(before) != _identity(
                        os.stat(library_path.name, dir_fd=directory, follow_symlinks=False)
                    ):
                        _fail("libzstd pathname changed before loading")
                    library = ctypes.CDLL(str(library_path))
                    symbol = ctypes.cast(library.ZSTD_versionString, ctypes.c_void_p).value
                    with open("/proc/self/maps", "rb") as mappings:
                        maps = mappings.read(MAX_METADATA_BYTES + 1)
                    if len(maps) > MAX_METADATA_BYTES or symbol is None:
                        _fail("loaded libzstd mapping evidence exceeds bound")
                    matched = False
                    for line in maps.decode("utf-8", errors="replace").splitlines():
                        columns = line.split(None, 5)
                        if len(columns) < 5:
                            _fail("invalid loaded-library mapping evidence")
                        start, end = (int(word, 16) for word in columns[0].split("-"))
                        if start <= symbol < end:
                            major, minor = (int(word, 16) for word in columns[3].split(":"))
                            matched = (
                                "x" in columns[1]
                                and "w" not in columns[1]
                                and os.makedev(major, minor) == before.st_dev
                                and int(columns[4]) == before.st_ino
                            )
                    if not matched or _identity(before) != _identity(os.fstat(source)):
                        _fail("loaded libzstd bytes do not match the hashed system-library inode")
                finally:
                    os.close(source)
            library.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            library.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
            library.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            library.ZSTD_findFrameCompressedSize.restype = ctypes.c_size_t
            library.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
            library.ZSTD_decompress.restype = ctypes.c_size_t
            library.ZSTD_isError.argtypes = [ctypes.c_size_t]
            library.ZSTD_isError.restype = ctypes.c_uint
            library.ZSTD_versionString.argtypes = []
            library.ZSTD_versionString.restype = ctypes.c_char_p

            version = library.ZSTD_versionString().decode("ascii")
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version) is None:
                _fail("unrecognized libzstd version")
            self.evidence = {
                "library": "libzstd.so.1",
                "version": version,
                "digest": _digest(library_bytes),
                "size": len(library_bytes),
            }
            self.library = library
            self.ctypes = ctypes
        except (OSError, AttributeError, UnicodeError) as exc:
            raise ArtifactValidationError(
                "VZ runtime assets: original .ko.zst inputs require existing system libzstd.so.1"
            ) from exc

    def decode(self, payload: bytes) -> bytes:
        if not 0 < len(payload) <= MAX_FILE_BYTES:
            _fail("zstd source module exceeds its byte bound")
        source = self.ctypes.create_string_buffer(payload)
        size = self.library.ZSTD_getFrameContentSize(source, len(payload))
        frame_size = self.library.ZSTD_findFrameCompressedSize(source, len(payload))
        if not 0 < size <= MAX_FILE_BYTES or self.library.ZSTD_isError(frame_size) or frame_size != len(payload):
            _fail("zstd module must have one complete frame with a known bounded output size")
        output = self.ctypes.create_string_buffer(size)
        decoded = self.library.ZSTD_decompress(output, size, source, len(payload))
        if self.library.ZSTD_isError(decoded) or decoded != size:
            _fail("zstd module decoded size or checksum is invalid")
        return output.raw


def _module_bytes(payload: bytes, path: str, zstd: _ZstdDecoder | None = None) -> bytes:
    if path.endswith(".ko.gz"):
        return _gunzip(payload, MAX_FILE_BYTES)
    if path.endswith(".ko.xz"):
        try:
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=MAX_ARCHIVE_BYTES)
            result = decoder.decompress(payload, max_length=MAX_FILE_BYTES + 1)
            if not decoder.eof or decoder.unused_data or len(result) > MAX_FILE_BYTES:
                _fail("xz module must be one complete bounded stream")
            return result
        except lzma.LZMAError:
            _fail("invalid xz kernel module")
    if path.endswith(".ko.zst"):
        if zstd is None:
            _fail("missing build-time zstd decoder")
        return zstd.decode(payload)
    if not path.endswith(".ko"):
        _fail("unsupported module compression; supply original .ko, .ko.gz, .ko.xz or .ko.zst files")
    return payload


def _module_output(path: str) -> str:
    _path(path)
    for suffix in (".gz", ".xz", ".zst"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path.endswith(".ko"):
        _fail("unsupported module filename")
    return path


def _module_name(path: str) -> str:
    name = _module_output(path).rsplit("/", 1)[-1][:-3].replace("-", "_")
    if _MODULE_NAME.fullmatch(name) is None:
        _fail("unsupported module name")
    return name


def _soft_dependencies(lines: list[str]) -> dict[str, dict[str, list[str]]]:
    soft: dict[str, dict[str, list[str]]] = {}
    for line in lines:
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        if len(words) < 3 or words[0] != "softdep" or _MODULE_NAME.fullmatch(words[1].replace("-", "_")) is None:
            _fail("invalid modules.softdep")
        entry = soft.setdefault(words[1].replace("-", "_"), {"pre:": [], "post:": []})
        phase: str | None = None
        for word in words[2:]:
            if word in {"pre:", "post:"}:
                phase = word
            elif phase is None:
                # depmod preserves unphased MODULE_SOFTDEP tokens; kmod ignores them.
                continue
            elif _MODULE_NAME.fullmatch(word.replace("-", "_")) is None:
                _fail("invalid soft module dependency name")
            else:
                entry[phase].append(word.replace("-", "_"))
    return soft


def _modules(
    source: int | dict[str, bytes],
    release: str,
    config: dict[str, str],
    *,
    source_indexes: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, bytes], list[dict[str, Any]], dict[str, Any] | None]:
    def read_module(path: str, limit: int) -> bytes:
        if isinstance(source, int):
            payload = _read_at(source, path, limit)
        else:
            if path not in source or len(source[path]) > limit:
                _fail("missing or oversized archived module input")
            payload = source[path]
        if source_indexes is not None and path in _MODULE_INDEXES:
            source_indexes[path] = _receipt(payload)
        return payload

    dep = read_module("modules.dep", MAX_METADATA_BYTES).decode("ascii")
    dependencies: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for line in dep.splitlines():
        path, sep, tail = line.partition(":")
        if not sep or path in dependencies:
            _fail("invalid modules.dep")
        _path(path)
        dependencies[path] = [_path(item) for item in tail.split()]
        name = _module_name(path)
        if name in names:
            _fail("ambiguous kernel module name")
        names[name] = path
    if len(dependencies) > 32768:
        _fail("module index exceeds bound")
    builtin = read_module("modules.builtin", MAX_METADATA_BYTES)
    builtin_paths = [_path(line) for line in builtin.decode("ascii").splitlines()]
    builtin_names = {_module_name(path) for path in builtin_paths}
    # Built-in objects can share a basename across directories (for example
    # arm-cci and clk-pll). This set answers availability, not payload selection.
    if builtin_names & names.keys():
        _fail("module is indexed as both built-in and loadable")
    selected: set[str] = set()
    visiting: set[str] = set()

    def include(path: str) -> None:
        if path in selected:
            return
        if path in visiting or path not in dependencies:
            _fail("cyclic or missing kernel module dependency")
        if len(selected) + len(visiting) >= MAX_ENTRIES - 32 or len(visiting) >= 128:
            _fail("module dependency closure exceeds bound")
        visiting.add(path)
        for dependency in dependencies[path]:
            include(dependency)
        visiting.remove(path)
        selected.add(path)

    required = {"CONFIG_VIRTIO_PCI": "virtio_pci", "CONFIG_VIRTIO_MMIO": "virtio_mmio", **_FEATURES}
    roots: list[str] = []
    for setting, name in required.items():
        if config.get(setting) == "m":
            if name not in names:
                _fail("required module is missing: " + name)
            roots.append(names[name])
            include(names[name])
        # CONFIG_*=y is authoritative for built-ins. modules.builtin describes
        # kbuild module objects, not every built-in core/config facility.
    softdep = read_module("modules.softdep", MAX_METADATA_BYTES)
    softlines = softdep.decode("ascii").splitlines()
    soft = _soft_dependencies(softlines)
    changed = True
    while changed:
        before = len(selected)
        for name in tuple(_module_name(path) for path in selected):
            for dependency in soft.get(name, {}).values():
                for target in dependency:
                    if target in names:
                        include(names[target])
                    elif target not in builtin_names:
                        _fail("missing soft module dependency")
        changed = len(selected) != before
    files: dict[str, bytes] = {}
    receipts: list[dict[str, Any]] = []
    outputs = {path: _module_output(path) for path in selected}
    total = 0
    source_total = 0
    zstd = _ZstdDecoder() if any(path.endswith(".ko.zst") for path in selected) else None
    for path in sorted(selected):
        original = read_module(path, MAX_FILE_BYTES)
        source_total += len(original)
        if source_total > MAX_ARCHIVE_BYTES:
            _fail("compressed module sources exceed aggregate byte bound")
        payload = _module_bytes(original, path, zstd)
        total += len(payload)
        if total > MAX_ARCHIVE_BYTES:
            _fail("module closure exceeds aggregate byte bound")
        receipt = _module(payload, release)
        tokens = receipt["vermagic"].split()
        for setting, token in (
            ("CONFIG_SMP", "SMP"),
            ("CONFIG_MODULE_UNLOAD", "mod_unload"),
            ("CONFIG_MODVERSIONS", "modversions"),
        ):
            if (config.get(setting) == "y") != (token in tokens):
                _fail("module vermagic contradicts original kernel config")
        if receipts and receipts[0]["vermagic"] != receipt["vermagic"]:
            _fail("module dependency closure mixes vermagic identities")
        declared = {word.replace("-", "_") for field in receipt["depends"] for word in field.split(",") if word}
        indexed = {_module_name(item) for item in dependencies[path]}
        if not declared.issubset(indexed | builtin_names):
            _fail("module modinfo dependencies contradict modules.dep")
        output = outputs[path]
        files[output] = payload
        receipts.append(
            {
                "path": output,
                "source_path": path,
                "source_encoding": path[len(output) + 1 :] if path != output else "ko",
                "source_size": len(original),
                "source_digest": _digest(original),
                **receipt,
            }
        )
    files["modules.dep"] = "".join(
        path
        + ":"
        + (" " + " ".join(outputs[item] for item in dependencies[source]) if dependencies[source] else "")
        + "\n"
        for source, path in sorted(outputs.items(), key=lambda pair: pair[1])
    ).encode()
    aliases = read_module("modules.alias", MAX_METADATA_BYTES).decode("ascii")
    selected_names = {_module_name(path) for path in selected}
    alias_lines: list[str] = []
    for line in aliases.splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        if len(words) != 3 or words[0] != "alias":
            _fail("invalid modules.alias")
        if words[2].replace("-", "_") in selected_names:
            alias_lines.append(line)
    files["modules.alias"] = ("\n".join(alias_lines) + "\n").encode()
    files["modules.softdep"] = (
        "\n".join(
            line
            for line in softlines
            if line.split() and line.split()[0] == "softdep" and line.split()[1].replace("-", "_") in selected_names
        )
        + "\n"
    ).encode()
    files["modules.builtin"] = builtin
    files["modules.builtin.modinfo"] = read_module("modules.builtin.modinfo", MAX_METADATA_BYTES)
    # BusyBox need not understand kmod softdep semantics: explicitly load the
    # complete closure in a topological order satisfying hard, pre and post edges.
    prerequisites = {path: set(dependencies[path]) for path in selected}
    for name, phases in soft.items():
        path = names.get(name)
        if path not in selected:
            continue
        for target in phases["pre:"]:
            if target in names:
                prerequisites[path].add(names[target])
        for target in phases["post:"]:
            if target in names:
                prerequisites[names[target]].add(path)
    ordered: list[str] = []
    done: set[str] = set()
    active: set[str] = set()

    def order(path: str) -> None:
        if path in done:
            return
        if path in active or len(active) >= 128:
            _fail("cyclic or excessive module load ordering")
        active.add(path)
        for prerequisite in sorted(prerequisites[path]):
            order(prerequisite)
        active.remove(path)
        done.add(path)
        ordered.append(_module_name(path))

    for path in [*roots, *sorted(selected)]:
        order(path)
    files["modules.load"] = "".join(name + "\n" for name in ordered).encode("ascii")
    # BusyBox uses text indexes; no stale binary depmod indexes are copied.
    return files, receipts, zstd.evidence if zstd is not None else None


def _record(inode: int, path: str, mode: int, data: bytes) -> bytes:
    name = path.encode("ascii") + b"\0"
    fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0)
    header = b"070701" + b"".join(f"{value:08x}".encode() for value in fields)
    return header + name + b"\0" * (-(110 + len(name)) % 4) + data + b"\0" * (-len(data) % 4)


def _archive(files: dict[str, bytes]) -> tuple[bytes, list[dict[str, Any]]]:
    entries: dict[str, tuple[int, bytes]] = {}
    for path, data in files.items():
        _path(path)
        if len(data) > MAX_FILE_BYTES:
            _fail("archive entry exceeds size bound")
        mode = 0o755 if path in {"init", *_COMPONENTS.values()} else 0o644
        entries[path] = (stat.S_IFREG | mode, data)
        for parent in PurePosixPath(path).parents:
            if str(parent) != ".":
                if str(parent) in files:
                    _fail("archive file shadows a directory")
                entries[str(parent)] = (stat.S_IFDIR | 0o755, b"")
    for path in ("dev", "proc", "sys", "run", "newroot"):
        entries[path] = (stat.S_IFDIR | 0o755, b"")
    if len(entries) > MAX_ENTRIES:
        _fail("archive entry count exceeds bound")
    records: list[bytes] = []
    receipts: list[dict[str, Any]] = []
    total = 0
    for index, (path, (mode, data)) in enumerate(sorted(entries.items()), 1):
        record = _record(index, path, mode, data)
        total += len(record)
        if total > MAX_ARCHIVE_BYTES:
            _fail("initramfs exceeds total size bound")
        records.append(record)
        receipts.append({"path": path, "mode": mode, "size": len(data), "digest": _digest(data)})
    records.append(_record(len(entries) + 1, "TRAILER!!!", 0, b""))
    return b"".join(records), receipts


def _parse_archive(payload: bytes) -> dict[str, bytes]:
    offset = 0
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    for index in range(MAX_ENTRIES + 1):
        if offset + 110 > len(payload) or payload[offset : offset + 6] != b"070701":
            _fail("initramfs newc header is invalid")
        raw = payload[offset + 6 : offset + 110]
        if any(byte not in b"0123456789abcdef" for byte in raw):
            _fail("noncanonical newc numeric field")
        fields = [int(raw[pos : pos + 8], 16) for pos in range(0, 104, 8)]
        inode, mode, uid, gid, links, mtime, size, devmajor, devminor, rmajor, rminor, namesize, checksum = fields
        if (
            inode != index + 1
            or uid
            or gid
            or links != 1
            or mtime
            or devmajor
            or devminor
            or rmajor
            or rminor
            or checksum
            or not 1 < namesize <= 1025
            or size > MAX_FILE_BYTES
        ):
            _fail("noncanonical newc metadata")
        start = offset + 110
        end = start + namesize
        data_start = (end + 3) & ~3
        data_end = data_start + size
        next_offset = (data_end + 3) & ~3
        if next_offset > len(payload) or payload[end - 1 : end] != b"\0":
            _fail("truncated newc entry")
        try:
            path = payload[start : end - 1].decode("ascii")
        except UnicodeError:
            _fail("non-ASCII newc path")
        data = payload[data_start:data_end]
        if payload[end:data_start].strip(b"\0") or payload[data_end:next_offset].strip(b"\0"):
            _fail("nonzero newc padding")
        offset = next_offset
        if path == "TRAILER!!!":
            if mode or size or offset != len(payload):
                _fail("invalid newc trailer")
            rebuilt, _receipts = _archive(files)
            if rebuilt != payload:
                _fail("initramfs layout/order is not canonical")
            return files
        _path(path)
        if path in seen:
            _fail("duplicate newc path")
        seen.add(path)
        if mode in {stat.S_IFREG | 0o644, stat.S_IFREG | 0o755}:
            files[path] = data
        elif mode != stat.S_IFDIR | 0o755 or size:
            _fail("unsupported newc entry type")
    _fail("initramfs entry count exceeds bound")


def _sidecar(path: Path) -> dict[str, Any] | None:
    companion = path.with_name(path.name + ".provenance.json")
    try:
        payload = _read(companion, MAX_METADATA_BYTES)
    except FileNotFoundError:
        return None
    return _provenance(_json(payload, canonical=False))


def _provenance(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) - {"source", "recipe", "toolchain", "licenses"}:
        _fail("component provenance permits only source, recipe, toolchain and licenses")

    # Reject host paths/secrets by construction: only public identifiers and digests.
    def public(item: Any, depth: int = 0) -> None:
        if depth > 8:
            _fail("component provenance nesting exceeds bound")
        if type(item) is str:
            if (
                len(item) > 4096
                or not item.isascii()
                or "\n" in item
                or "\0" in item
                or item.startswith(("/", "~", "file:"))
                or "PRIVATE KEY" in item
                or re.search(r"https?://[^/]*@", item)
            ):
                _fail("component provenance must contain only public metadata, not paths or secrets")
        elif type(item) is dict:
            for key, child in item.items():
                public(key, depth + 1)
                public(child, depth + 1)
        elif type(item) is list:
            for child in item:
                public(child, depth + 1)
        elif item is not None and type(item) not in {bool, int}:
            _fail("unsupported provenance value")

    public(value)
    return value


def _receipt(payload: bytes) -> dict[str, Any]:
    return {"digest": _digest(payload), "size": len(payload)}


def build_vz_runtime_assets(
    output_dir: Path,
    *,
    platform_binary: Path,
    kernel: Path,
    modules_dir: Path,
    busybox: Path,
    dropbear: Path,
    guestagent: Path,
    kernel_release: str,
    lima_version: str = "2.2.1",
) -> Path:
    """Assemble a NEW private directory from supplied offline Linux inputs.

    The original Ubuntu kernel may be Image or a single gzip stream; the output
    is always uncompressed Image. Config discovery is limited to ``config`` in
    modules_dir and ``config-<release>`` beside the supplied kernel. Module
    indexes must come from that same installation; symlinks/build trees are not
    traversed. Existing output directories are never modified or adopted.
    """
    if sys.platform != "linux":
        raise UnsupportedPlatformError("VZ runtime asset assembly requires an offline Linux builder")
    if type(kernel_release) is not str or _RELEASE.fullmatch(kernel_release) is None or lima_version != "2.2.1":
        _fail("a bounded exact kernel release and Lima 2.2.1 are required")
    kernel = Path(kernel)
    modules_dir = Path(modules_dir)
    if modules_dir.name != kernel_release:
        _fail("modules directory must be the exact supplied kernel release")
    original_kernel = _read(kernel, MAX_KERNEL_BYTES)
    image = _gunzip(original_kernel, MAX_KERNEL_BYTES) if original_kernel.startswith(b"\x1f\x8b") else original_kernel
    banner = _image(image, kernel_release)
    config_candidates = (modules_dir / "config", kernel.parent / ("config-" + kernel_release))
    config_bytes: bytes | None = None
    for candidate in config_candidates:
        try:
            found = _read(candidate, MAX_METADATA_BYTES)
        except FileNotFoundError:
            continue
        if config_bytes is not None and found != config_bytes:
            _fail("supplied original kernel configs disagree")
        config_bytes = found
    if config_bytes is None:
        _fail("original matching kernel config is required (modules_dir/config or config-<release> beside kernel)")
    config = _config(config_bytes)
    config_binding = _kernel_config_binding(image, config_bytes)
    config_release = config.get("CONFIG_LOCALVERSION", '""').strip('"')
    if config_release and not kernel_release.endswith(config_release):
        _fail("kernel config local version contradicts kernel release")
    with _directory(modules_dir) as fd:
        source_indexes: dict[str, dict[str, Any]] = {}
        module_files, module_receipts, zstd_evidence = _modules(
            fd, kernel_release, config, source_indexes=source_indexes
        )
    module_load = module_files.pop("modules.load")
    files: dict[str, bytes] = {
        "lib/modules/" + kernel_release + "/" + path: data for path, data in module_files.items()
    }
    files["lib/modules/" + kernel_release + "/config"] = config_bytes
    files["platform/modules.load"] = module_load
    components: dict[str, Any] = {}
    for role, path in {
        "platform": platform_binary,
        "busybox": busybox,
        "dropbear": dropbear,
        "guestagent": guestagent,
    }.items():
        payload = _read(Path(path), MAX_FILE_BYTES)
        components[role] = {**_executable(payload, role, lima_version), "provenance": _sidecar(Path(path))}
        files[_COMPONENTS[role]] = payload
    files["init"] = files["platform/plm-vz"]
    archive, entries = _archive(files)
    # Fixed header, no filename, zero mtime; zlib output is pinned by its manifest.
    compressed = gzip.compress(archive, compresslevel=9, mtime=0)
    compressed = compressed[:9] + b"\xff" + compressed[10:]
    manifest = {
        "schema": SCHEMA,
        "architecture": "aarch64",
        "platform_abi": PLATFORM_ABI,
        "kernel_release": kernel_release,
        "lima_version": lima_version,
        "kernel": {
            **_receipt(image),
            "source_digest": _digest(original_kernel),
            "source_size": len(original_kernel),
            "source_encoding": "gzip" if original_kernel.startswith(b"\x1f\x8b") else "Image",
            "banner": banner,
            "config_digest": _digest(config_bytes),
            "config_binding": config_binding,
            "provenance": _sidecar(kernel),
        },
        "initramfs": {**_receipt(compressed), "encoding": "gzip-newc", "uncompressed": _receipt(archive)},
        "components": components,
        "modules": module_receipts,
        "entries": entries,
        "recipe": {
            "id": SCHEMA,
            "cpio": "newc-sorted-root-zero-time-singlelink-v1",
            "gzip": "level9-mtime0-no-filename-os255",
            "zlib_version": zlib.ZLIB_VERSION,
            "module_zstd_decoder": zstd_evidence,
            "module_index_sources": source_indexes,
            "module_policy": "original-matched-vermagic-ordered-text-softdep-load-closure-v2",
            "qualification": "offline-structural-validation-only-no-native-runtime-attestation",
        },
    }
    manifest_bytes = canonical_json_bytes(manifest)
    if len(manifest_bytes) > MAX_METADATA_BYTES:
        _fail("manifest exceeds metadata bound")
    output_dir = Path(os.path.abspath(Path(output_dir).expanduser()))
    with _directory(output_dir.parent) as parent:
        os.mkdir(output_dir.name, 0o700, dir_fd=parent)
        fd = os.open(output_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        directory_info = os.fstat(fd)
        directory_identity = (directory_info.st_dev, directory_info.st_ino)
        created: dict[str, tuple[int, int]] = {}
        try:
            for name, payload in (
                ("Image", image),
                ("initramfs.cpio.gz", compressed),
                ("manifest.json", manifest_bytes),
            ):
                target = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=fd
                )
                target_info = os.fstat(target)
                created[name] = (target_info.st_dev, target_info.st_ino)
                with os.fdopen(target, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchmod(stream.fileno(), 0o400)
            os.fchmod(fd, 0o500)
            os.fsync(fd)
            os.fsync(parent)
            published = load_vz_runtime_assets(output_dir)
            if (
                published.manifest_digest != _digest(manifest_bytes)
                or (published.root_device, published.root_inode) != directory_identity
            ):
                _fail("copied output differs from the assembled directory/manifest")
        except BaseException:
            os.fchmod(fd, 0o700)
            for name, identity in created.items():
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) == identity and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    os.unlink(name, dir_fd=fd)
            info = os.stat(output_dir.name, dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == directory_identity and not os.listdir(fd):
                os.rmdir(output_dir.name, dir_fd=parent)
            raise
        finally:
            os.close(fd)
    return output_dir


def _match_receipt(payload: bytes, receipt: Any) -> None:
    if (
        type(receipt) is not dict
        or type(receipt.get("size")) is not int
        or receipt.get("size") != len(payload)
        or receipt.get("digest") != _digest(payload)
    ):
        _fail("asset size/digest receipt does not match pinned bytes")


def load_vz_runtime_assets(path: Path) -> VZRuntimeAssets:
    """Reopen only a complete immutable owner-private asset directory, offline."""
    root = Path(os.path.abspath(Path(path).expanduser()))
    try:
        with _directory(root, private=True) as fd:
            if set(os.listdir(fd)) != _OUTPUT:
                _fail("asset directory has missing or unexpected files")
            identities = {name: _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) for name in _OUTPUT}
            manifest_bytes = _read_at(fd, "manifest.json", MAX_METADATA_BYTES, immutable=True)
            manifest = _json(manifest_bytes)
            keys = {
                "schema",
                "architecture",
                "platform_abi",
                "kernel_release",
                "lima_version",
                "kernel",
                "initramfs",
                "components",
                "modules",
                "entries",
                "recipe",
            }
            if (
                type(manifest) is not dict
                or set(manifest) != keys
                or manifest["schema"] != SCHEMA
                or manifest["architecture"] != "aarch64"
                or manifest["platform_abi"] != PLATFORM_ABI
                or manifest["lima_version"] != "2.2.1"
                or type(manifest["kernel_release"]) is not str
                or _RELEASE.fullmatch(manifest["kernel_release"]) is None
            ):
                _fail("unsupported or incomplete asset manifest")
            release = manifest["kernel_release"]
            image = _read_at(fd, "Image", MAX_KERNEL_BYTES, immutable=True)
            compressed = _read_at(fd, "initramfs.cpio.gz", MAX_ARCHIVE_BYTES, immutable=True)
            _match_receipt(image, manifest["kernel"])
            _match_receipt(compressed, manifest["initramfs"])
            kernel_keys = {
                "digest",
                "size",
                "source_digest",
                "source_size",
                "source_encoding",
                "banner",
                "config_digest",
                "config_binding",
                "provenance",
            }
            if (
                set(manifest["kernel"]) != kernel_keys
                or manifest["kernel"]["banner"] != _image(image, release)
                or _DIGEST.fullmatch(manifest["kernel"]["source_digest"]) is None
                or type(manifest["kernel"]["source_size"]) is not int
                or not 0 < manifest["kernel"]["source_size"] <= MAX_KERNEL_BYTES
                or (
                    manifest["kernel"]["source_encoding"] == "Image"
                    and (
                        manifest["kernel"]["source_digest"] != _digest(image)
                        or manifest["kernel"]["source_size"] != len(image)
                    )
                )
                or manifest["kernel"]["source_encoding"] not in {"Image", "gzip"}
                or set(manifest["initramfs"]) != {"digest", "size", "encoding", "uncompressed"}
                or manifest["initramfs"]["encoding"] != "gzip-newc"
            ):
                _fail("kernel/initramfs manifest identity is invalid")
            archive = _gunzip(compressed, MAX_ARCHIVE_BYTES)
            _match_receipt(archive, manifest["initramfs"]["uncompressed"])
            files = _parse_archive(archive)
            _rebuilt, entries = _archive(files)
            if canonical_json_bytes(entries) != canonical_json_bytes(manifest["entries"]):
                _fail("archive receipts differ from manifest")
            if type(manifest["components"]) is not dict or set(manifest["components"]) != set(_COMPONENTS):
                _fail("component manifest is incomplete")
            if files.get("init") != files.get("platform/plm-vz"):
                _fail("PID1 and multicall platform bytes differ")
            allowed = {"init", *_COMPONENTS.values()}
            for role, filename in _COMPONENTS.items():
                if filename not in files:
                    _fail("missing platform executable")
                expected = _executable(files[filename], role, manifest["lima_version"])
                receipt = manifest["components"][role]
                if (
                    type(receipt) is not dict
                    or set(receipt) != {*expected, "provenance"}
                    or any(receipt[key] != value for key, value in expected.items())
                ):
                    _fail("component identity differs from manifest")
                _provenance(receipt["provenance"])
            prefix = "lib/modules/" + release + "/"
            if prefix + "config" not in files:
                _fail("archive lacks original kernel config")
            config_bytes = files[prefix + "config"]
            config = _config(config_bytes)
            allowed.add("platform/modules.load")
            if "platform/modules.load" not in files:
                _fail("archive lacks the config-bound explicit module load order")
            if _digest(config_bytes) != manifest["kernel"]["config_digest"]:
                _fail("kernel config digest mismatch")
            if manifest["kernel"]["config_binding"] != _kernel_config_binding(image, config_bytes):
                _fail("kernel config binding differs from embedded kernel evidence")
            _provenance(manifest["kernel"]["provenance"])
            allowed.update(
                prefix + name
                for name in (
                    "config",
                    "modules.dep",
                    "modules.alias",
                    "modules.softdep",
                    "modules.builtin",
                    "modules.builtin.modinfo",
                )
            )
            modules = manifest["modules"]
            if (
                type(modules) is not list
                or len(modules) > MAX_ENTRIES
                or modules != sorted(modules, key=lambda item: item["path"])
            ):
                _fail("module manifest is invalid")
            for receipt in modules:
                name = _path(receipt["path"])
                filename = prefix + name
                if filename not in files or not name.endswith(".ko") or filename in allowed:
                    _fail("module manifest is ambiguous or incomplete")
                allowed.add(filename)
                expected = _module(files[filename], release)
                if (
                    set(receipt)
                    != {"path", "source_path", "source_size", "source_encoding", "source_digest", *expected}
                    or _DIGEST.fullmatch(receipt["source_digest"]) is None
                    or type(receipt["source_size"]) is not int
                    or not 0 < receipt["source_size"] <= MAX_FILE_BYTES
                    or receipt["source_encoding"] not in {"ko", "gz", "xz", "zst"}
                    or _module_output(receipt["source_path"]) != name
                    or receipt["source_path"]
                    != name + ("" if receipt["source_encoding"] == "ko" else "." + receipt["source_encoding"])
                    or (
                        receipt["source_encoding"] == "ko"
                        and (
                            receipt["source_digest"] != expected["digest"] or receipt["source_size"] != expected["size"]
                        )
                    )
                    or any(receipt[key] != value for key, value in expected.items())
                ):
                    _fail("module provenance differs from archive")
            if set(files) != allowed:
                _fail("initramfs contains unapproved or missing content")
            module_inputs = {name[len(prefix) :]: data for name, data in files.items() if name.startswith(prefix)}
            module_inputs["modules.load"] = files["platform/modules.load"]
            closure, _closure_receipts, _decoder = _modules(module_inputs, release, config)
            if closure != {name: data for name, data in module_inputs.items() if name != "config"}:
                _fail("archived module dependency closure or text indexes are incomplete/noncanonical")
            expected_recipe = {
                "id",
                "cpio",
                "gzip",
                "zlib_version",
                "module_zstd_decoder",
                "module_index_sources",
                "module_policy",
                "qualification",
            }
            if (
                type(manifest["recipe"]) is not dict
                or set(manifest["recipe"]) != expected_recipe
                or manifest["recipe"]["id"] != SCHEMA
                or manifest["recipe"]["cpio"] != "newc-sorted-root-zero-time-singlelink-v1"
                or manifest["recipe"]["gzip"] != "level9-mtime0-no-filename-os255"
                or type(manifest["recipe"]["zlib_version"]) is not str
                or manifest["recipe"]["module_policy"]
                != "original-matched-vermagic-ordered-text-softdep-load-closure-v2"
                or manifest["recipe"]["qualification"]
                != "offline-structural-validation-only-no-native-runtime-attestation"
            ):
                _fail("unsupported assembly recipe")
            indexes = manifest["recipe"]["module_index_sources"]
            if type(indexes) is not dict or set(indexes) != set(_MODULE_INDEXES):
                _fail("original module-index provenance is incomplete")
            for name, receipt in indexes.items():
                if (
                    type(receipt) is not dict
                    or set(receipt) != {"digest", "size"}
                    or type(receipt["digest"]) is not str
                    or _DIGEST.fullmatch(receipt["digest"]) is None
                    or type(receipt["size"]) is not int
                    or not 0 <= receipt["size"] <= MAX_METADATA_BYTES
                ):
                    _fail("invalid original module-index receipt")
                if name in {"modules.builtin", "modules.builtin.modinfo"}:
                    _match_receipt(module_inputs[name], receipt)
            decoder = manifest["recipe"]["module_zstd_decoder"]
            if decoder is not None and (
                type(decoder) is not dict
                or set(decoder) != {"library", "version", "digest", "size"}
                or decoder["library"] != "libzstd.so.1"
                or type(decoder["version"]) is not str
                or re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", decoder["version"]) is None
                or type(decoder["digest"]) is not str
                or _DIGEST.fullmatch(decoder["digest"]) is None
                or type(decoder["size"]) is not int
                or not 0 < decoder["size"] <= MAX_FILE_BYTES
            ):
                _fail("invalid build-time zstd library evidence")
            if any(receipt["source_encoding"] == "zst" for receipt in modules) != (decoder is not None):
                _fail("zstd decoder evidence disagrees with original module encodings")
            if compressed[:10] != b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff":
                _fail("initramfs gzip header is not deterministic")
            if set(os.listdir(fd)) != _OUTPUT:
                _fail("asset directory changed during validation")
            if identities != {name: _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) for name in _OUTPUT}:
                _fail("published asset identity changed during validation")
            info = os.fstat(fd)
            return VZRuntimeAssets(
                root,
                root / "Image",
                root / "initramfs.cpio.gz",
                _digest(manifest_bytes),
                release,
                manifest["lima_version"],
                info.st_dev,
                info.st_ino,
            )
    except ArtifactValidationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, struct.error, RecursionError) as exc:
        raise ArtifactValidationError("VZ runtime assets: unavailable, unsafe or malformed offline assets") from exc
