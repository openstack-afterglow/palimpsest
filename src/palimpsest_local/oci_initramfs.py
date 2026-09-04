"""Deterministic, structurally verified OCI-root stage-1 initramfs artifacts.

The first-party static ``/init`` authenticates the raw stage-1 transport,
block-device identities, root ext4 structure, and immutable lower SquashFS
structure/content, mounts them, assembles OverlayFS, verifies optional
authenticated root-level probes, and performs an initramfs-safe
move-mount/chroot root transition, then executes and supervises the admitted
image process before entering a terminal fail-closed wait. It deliberately
does not call ``pivot_root(2)``. Exact source, toolchain,
sealing recipe, binary and ABI provenance prevents transport consumption from
being confused with full OCI-root boot readiness.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import re
import stat
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .digest import normalize_digest
from .errors import ArtifactValidationError
from .oci_guest_stage1 import (
    OCI_GUEST_STAGE1_CAPABILITY,
    OCI_GUEST_STAGE1_CONTRACT,
    OCI_GUEST_STAGE1_PLAN_TRANSPORT,
)
from .oci_provenance import canonical_json_bytes

OCI_INITRAMFS_MANIFEST_SCHEMA = "palimpsest.oci-initramfs-manifest.v15"
OCI_INITRAMFS_GENERATOR_CONTRACT = "palimpsest.initramfs.newc.v1"
OCI_BOOTSTRAP_STAGE1_CONTRACT = "palimpsest.guest-stage1-init.x86_64.v13"
OCI_STAGE1_ABI = "palimpsest.guest-stage1-bootstrap.v15"
OCI_STAGE1_ROOT_TRANSITION_CONTRACT = "palimpsest.stage1-root-transition.v1"
OCI_STAGE1_SUPERVISOR_CONTRACT = "palimpsest.guest-pid1-supervisor.v6"
OCI_STAGE1_LIFECYCLE_BROKER_CONTRACT = "palimpsest.guest-lifecycle-broker.v2"
OCI_STAGE1_WORKLOAD_ISOLATION_CONTRACT = "palimpsest.workload-lifecycle-authority-isolation.v1"
OCI_STAGE1_PLAN_TRANSPORT = OCI_GUEST_STAGE1_PLAN_TRANSPORT
OCI_BOOTSTRAP_CAPABILITY = OCI_GUEST_STAGE1_CAPABILITY
OCI_STAGE1_BUILD_CONTRACT = "palimpsest.guest-stage1-build-sealed-elf.v1"
OCI_STAGE1_TOOLCHAIN_IMAGE = (
    "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1"
)
OCI_STAGE1_SOURCE_DIGEST = "sha256:7ddf6f8d0eae060ac889fc959e3c6ac7ef2273504a37a1b412da5f3624fe2e30"
OCI_STAGE1_BUILD_RECIPE_DIGEST = "sha256:c8bcfa444a295ed05a05b04340b221a466df9b383c0fa659160c869a892777b9"
OCI_STAGE1_SEAL_RECIPE_DIGEST = "sha256:f103ba852593d4c242ddd9f7f62a8ea043b18f6f5c72399eda6811925edfb196"
OCI_STAGE1_BINARY_DIGEST = "sha256:d7f667e255fdf6eb797741eff71fe2d8252065f501624b30cae6a04e884be5a6"
MAX_OCI_INITRAMFS_BYTES = 64 * 1024 * 1024
MAX_OCI_INITRAMFS_ENTRY_BYTES = 32 * 1024 * 1024
MAX_OCI_INITRAMFS_ENTRIES = 64

_NEWC_MAGIC = b"070701"
_NEWC_HEADER_BYTES = 110
_TRAILER = "TRAILER!!!"
_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_REQUIRED_LAYOUT = (
    ("dev", stat.S_IFDIR | 0o755),
    ("etc", stat.S_IFDIR | 0o755),
    ("etc/palimpsest", stat.S_IFDIR | 0o755),
    ("etc/palimpsest/guest-stage1-consumer.json", stat.S_IFREG | 0o644),
    ("etc/palimpsest/stage1-abi.json", stat.S_IFREG | 0o644),
    ("init", stat.S_IFREG | 0o755),
    ("proc", stat.S_IFDIR | 0o755),
    ("run", stat.S_IFDIR | 0o755),
    ("run/palimpsest", stat.S_IFDIR | 0o755),
    ("run/palimpsest/lowers", stat.S_IFDIR | 0o755),
    ("run/palimpsest/merged", stat.S_IFDIR | 0o755),
    ("run/palimpsest/root", stat.S_IFDIR | 0o755),
    ("sys", stat.S_IFDIR | 0o755),
)
_REQUIRED_PATHS = tuple(path for path, _mode in _REQUIRED_LAYOUT)


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field_name} is invalid")
    try:
        normalized = normalize_digest(value)
    except (ArtifactValidationError, TypeError, ValueError):
        raise ArtifactValidationError(f"{field_name} is invalid") from None
    if normalized != value:
        raise ArtifactValidationError(f"{field_name} is not canonical")
    return normalized


def _canonical_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\0" in value
        or "\\" in value
        or value.startswith("/")
        or _PATH_RE.fullmatch(value) is None
        or any(component in {".", ".."} for component in value.split("/"))
    ):
        raise ArtifactValidationError("initramfs entry path is invalid")
    return value


@dataclass(frozen=True, slots=True)
class NewcEntry:
    path: str
    mode: int
    data: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _canonical_path(self.path))
        if type(self.mode) is not int or self.mode not in {
            stat.S_IFDIR | 0o755,
            stat.S_IFREG | 0o644,
            stat.S_IFREG | 0o755,
        }:
            raise ArtifactValidationError("initramfs entry mode is unsupported")
        if not isinstance(self.data, bytes) or len(self.data) > MAX_OCI_INITRAMFS_ENTRY_BYTES:
            raise ArtifactValidationError("initramfs entry data is invalid")
        if stat.S_ISDIR(self.mode) and self.data:
            raise ArtifactValidationError("initramfs directory data must be empty")


def _padding(size: int) -> bytes:
    return b"\0" * ((-size) % 4)


def _newc_record(*, inode: int, mode: int, path: str, data: bytes) -> bytes:
    name = path.encode("ascii") + b"\0"
    fields = (
        inode,
        mode,
        0,
        0,
        1,
        0,
        len(data),
        0,
        0,
        0,
        0,
        len(name),
        0,
    )
    header = _NEWC_MAGIC + b"".join(f"{field:08x}".encode("ascii") for field in fields)
    return header + name + _padding(len(header) + len(name)) + data + _padding(len(data))


def build_newc(entries: Sequence[NewcEntry]) -> bytes:
    """Return one canonical uncompressed ``newc`` archive."""

    if not isinstance(entries, (tuple, list)) or not 1 <= len(entries) <= MAX_OCI_INITRAMFS_ENTRIES:
        raise ArtifactValidationError("initramfs entries are invalid")
    canonical = tuple(entries)
    if any(not isinstance(entry, NewcEntry) for entry in canonical):
        raise ArtifactValidationError("initramfs entry is invalid")
    paths = tuple(entry.path for entry in canonical)
    if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
        raise ArtifactValidationError("initramfs entries must be unique and lexically ordered")
    directories = {entry.path for entry in canonical if stat.S_ISDIR(entry.mode)}
    for path in paths:
        components = path.split("/")
        for index in range(1, len(components)):
            if "/".join(components[:index]) not in directories:
                raise ArtifactValidationError("initramfs entry parent directory is missing")
    records = [
        _newc_record(inode=index, mode=entry.mode, path=entry.path, data=entry.data)
        for index, entry in enumerate(canonical, 1)
    ]
    records.append(_newc_record(inode=len(canonical) + 1, mode=0, path=_TRAILER, data=b""))
    payload = b"".join(records)
    if len(payload) > MAX_OCI_INITRAMFS_BYTES:
        raise ArtifactValidationError("initramfs archive is too large")
    return payload


def _hex_field(raw: bytes) -> int:
    if len(raw) != 8 or any(value not in b"0123456789abcdef" for value in raw):
        raise ArtifactValidationError("initramfs newc header is not canonical")
    return int(raw, 16)


def parse_newc(payload: bytes) -> tuple[NewcEntry, ...]:
    """Parse only the canonical ``newc`` subset emitted by :func:`build_newc`."""

    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_OCI_INITRAMFS_BYTES:
        raise ArtifactValidationError("initramfs archive bytes are invalid")
    offset = 0
    entries: list[NewcEntry] = []
    trailer_seen = False
    while offset < len(payload):
        start = offset
        if len(payload) - offset < _NEWC_HEADER_BYTES or payload[offset : offset + 6] != _NEWC_MAGIC:
            raise ArtifactValidationError("initramfs newc header is invalid")
        header = payload[offset + 6 : offset + _NEWC_HEADER_BYTES]
        fields = tuple(_hex_field(header[index : index + 8]) for index in range(0, len(header), 8))
        (
            inode,
            mode,
            uid,
            gid,
            nlink,
            mtime,
            filesize,
            devmajor,
            devminor,
            rdevmajor,
            rdevminor,
            namesize,
            check,
        ) = fields
        offset += _NEWC_HEADER_BYTES
        if namesize < 2 or namesize > 4097 or offset + namesize > len(payload):
            raise ArtifactValidationError("initramfs newc name is invalid")
        raw_name = payload[offset : offset + namesize]
        if not raw_name.endswith(b"\0") or b"\0" in raw_name[:-1]:
            raise ArtifactValidationError("initramfs newc name is invalid")
        try:
            path = raw_name[:-1].decode("ascii")
        except UnicodeDecodeError:
            raise ArtifactValidationError("initramfs newc name is invalid") from None
        offset += namesize
        name_padding = (-(_NEWC_HEADER_BYTES + namesize)) % 4
        if offset + name_padding > len(payload) or payload[offset : offset + name_padding] != b"\0" * name_padding:
            raise ArtifactValidationError("initramfs newc name padding is invalid")
        offset += name_padding
        if filesize > MAX_OCI_INITRAMFS_ENTRY_BYTES or offset + filesize > len(payload):
            raise ArtifactValidationError("initramfs newc data is invalid")
        data = payload[offset : offset + filesize]
        offset += filesize
        data_padding = (-filesize) % 4
        if offset + data_padding > len(payload) or payload[offset : offset + data_padding] != b"\0" * data_padding:
            raise ArtifactValidationError("initramfs newc data padding is invalid")
        offset += data_padding
        expected_inode = len(entries) + 1
        if (
            inode != expected_inode
            or uid != 0
            or gid != 0
            or nlink != 1
            or mtime != 0
            or devmajor != 0
            or devminor != 0
            or rdevmajor != 0
            or rdevminor != 0
            or check != 0
        ):
            raise ArtifactValidationError("initramfs newc metadata is not canonical")
        if path == _TRAILER:
            if mode != 0 or filesize != 0 or offset != len(payload):
                raise ArtifactValidationError("initramfs newc trailer is invalid")
            trailer_seen = True
            break
        entries.append(NewcEntry(path, mode, data))
        if len(entries) > MAX_OCI_INITRAMFS_ENTRIES:
            raise ArtifactValidationError("initramfs archive has too many entries")
        if offset <= start:  # pragma: no cover - defensive monotonicity guard
            raise ArtifactValidationError("initramfs parser made no progress")
    if not trailer_seen:
        raise ArtifactValidationError("initramfs newc trailer is missing")
    result = tuple(entries)
    if build_newc(result) != payload:
        raise ArtifactValidationError("initramfs archive is not canonical")
    return result


def verify_static_x86_64_elf(payload: bytes) -> None:
    """Require a standalone static x86_64 ELF executable with an RX entry."""

    if not isinstance(payload, bytes) or not 64 <= len(payload) <= MAX_OCI_INITRAMFS_ENTRY_BYTES:
        raise ArtifactValidationError("stage-1 ELF bytes are invalid")
    try:
        unpacked = struct.unpack_from("<16sHHIQQQIHHHHHH", payload)
    except struct.error:
        raise ArtifactValidationError("stage-1 ELF header is invalid") from None
    (
        ident,
        elf_type,
        machine,
        version,
        entry,
        phoff,
        shoff,
        flags,
        ehsize,
        phentsize,
        phnum,
        shentsize,
        shnum,
        shstrndx,
    ) = unpacked
    if (
        ident[:7] != b"\x7fELF\x02\x01\x01"
        or ident[7] not in {0, 3}
        or ident[8:] != b"\0" * 8
        or elf_type != 2
        or machine != 62
        or version != 1
        or flags != 0
        or ehsize != 64
        or phentsize != 56
        or not 1 <= phnum <= 128
        or phoff < 64
        or phoff + phentsize * phnum > len(payload)
        or shoff != 0
        or shentsize != 0
        or shnum != 0
        or shstrndx != 0
    ):
        raise ArtifactValidationError("stage-1 ELF identity is invalid")
    executable_entry = False
    stack_policy_seen = False
    for index in range(phnum):
        try:
            program = struct.unpack_from("<IIQQQQQQ", payload, phoff + index * phentsize)
        except struct.error:
            raise ArtifactValidationError("stage-1 ELF program header is invalid") from None
        kind, program_flags, file_offset, virtual, _physical, file_size, memory_size, alignment = program
        if program_flags & ~7:
            raise ArtifactValidationError("stage-1 ELF program flags are invalid")
        if kind in {2, 3}:
            raise ArtifactValidationError("stage-1 ELF must not contain a dynamic loader")
        if (
            file_size > memory_size
            or memory_size > MAX_OCI_INITRAMFS_ENTRY_BYTES
            or file_offset + file_size > len(payload)
        ):
            raise ArtifactValidationError("stage-1 ELF segment is invalid")
        if alignment not in {0, 1} and (alignment & (alignment - 1)) != 0:
            raise ArtifactValidationError("stage-1 ELF alignment is invalid")
        if alignment not in {0, 1} and file_offset % alignment != virtual % alignment:
            raise ArtifactValidationError("stage-1 ELF segment alignment is invalid")
        if kind == 1 and program_flags & 3 == 3:
            raise ArtifactValidationError("stage-1 ELF load segment must not be writable and executable")
        if kind == 0x6474E551:
            if stack_policy_seen or program_flags & 1 or file_size != 0 or memory_size != 0:
                raise ArtifactValidationError("stage-1 ELF stack policy is invalid")
            stack_policy_seen = True
        if kind == 1 and program_flags & 5 == 5 and virtual <= entry < virtual + file_size:
            executable_entry = True
    if not executable_entry:
        raise ArtifactValidationError("stage-1 ELF entry is not executable")
    if not stack_policy_seen:
        raise ArtifactValidationError("stage-1 ELF stack policy is missing")


def _bootstrap_stage1_binary() -> bytes:
    """Load the packaged, compiler-free-at-runtime first-party stage-1 ELF."""

    try:
        payload = (
            importlib.resources.files("palimpsest_local").joinpath("assets", "oci-stage1-init.x86_64").read_bytes()
        )
    except (FileNotFoundError, OSError):
        raise ArtifactValidationError("packaged stage-1 ELF asset is missing") from None
    if _digest(payload) != OCI_STAGE1_BINARY_DIGEST:
        raise ArtifactValidationError("packaged stage-1 ELF digest is invalid")
    verify_static_x86_64_elf(payload)
    return payload


def _supervisor_contract_dict() -> dict[str, Any]:
    return {
        "cgroup": "fd-pinned-cgroup-v2-palimpsest.workload",
        "cgroup_security": "private-readonly-view-plus-dedicated-cleanup-authority",
        "cleanup_scope": "dedicated-workload-cgroup",
        "contract": OCI_STAGE1_SUPERVISOR_CONTRACT,
        "credential_transition": "isolate-drop-verify-handshake-cgroup-attach-release",
        "credentials": "root-pid1-broker-and-capabilityless-admitted-numeric-workload-identity",
        "environment": "authenticated-image-environment-only",
        "execution": "fork-isolation-ready-cgroup-attach-release-gate-execve-cloexec-error-pipe",
        "isolation": {
            "capabilities": "empty-bounding-ambient-permitted-effective-inheritable",
            "contract": OCI_STAGE1_WORKLOAD_ISOLATION_CONTRACT,
            "devices": ["full", "null", "random", "tty", "urandom", "zero"],
            "lifecycle_fd": "child-closed-before-isolation-ready",
            "mounts": "private-dev-tmpfs-masked-virtio-ports-readonly-proc-sys-cgroup",
            "pid1_proc": "nondumpable-before-fork",
            "seccomp": "authority-escape-boundary-filter",
        },
        "lifecycle_broker": OCI_STAGE1_LIFECYCLE_BROKER_CONTRACT,
        "lifecycle_delivery": "reconnect-snapshot-same-id-stop-retry-v1",
        "privilege_after_fork": "root-pid1-narrow-broker-capabilityless-workload",
        "signal_transport": "blocked-signalfd-process-group-forwarding",
        "production_cleanup": "stop-signal-grace-cgroup.kill-wait4-echild-populated-zero-rmdir",
        "terminal_state": "parent-marker-then-fail-closed-wait",
        "wait": "wait4-to-echild-with-empty-cgroup-proof",
    }


def _guest_consumer_contract_bytes() -> bytes:
    return canonical_json_bytes(
        {
            "capability": OCI_GUEST_STAGE1_CAPABILITY,
            "contract": OCI_GUEST_STAGE1_CONTRACT,
            "embedded_in_init": True,
            "plan_transport": OCI_GUEST_STAGE1_PLAN_TRANSPORT,
            "pivot_root": False,
            "root_assembly": True,
            "root_is_slash": True,
            "root_transition": {
                "contract": OCI_STAGE1_ROOT_TRANSITION_CONTRACT,
                "method": "move-mount-chroot",
                "pid1_root_matches_slash": True,
                "pivot_root": False,
                "pseudo_filesystems": ["dev", "sys", "proc"],
                "root_filesystem": "overlay",
                "switch_root": True,
                "workload_started": False,
            },
            "supervisor": _supervisor_contract_dict(),
            "switch_root": True,
            "validation": [
                "bounded-closed-world-kernel-cmdline",
                "proc-sysfs-devtmpfs-pseudo-filesystems",
                "unique-virtio-blk-read-only-major-minor-ioctl-identity",
                "raw-envelope-header-sha256-zero-padding",
                "canonical-json-exact-stage1-semantics",
                "resource-core-transport-root-lowers-cross-binding",
                "root-lower-role-size-readonly-open-fd-final-recheck",
                "ext4-primary-superblock-uuid-label-geometry-features-checksum",
                "squashfs-v4-structure-whole-device-sha256-byte-budget",
                "proc-fd-bound-ext4-squashfs-mount-source",
                "nofollow-staging-directories-mountinfo-statfs-final-recheck",
                "highest-ordinal-leftmost-overlay-upper-work-assembly",
                "move-mounted-root-chroot-pid1-root-identity",
                "root-owned-0755-empty-pseudo-filesystem-move-targets",
                "moved-proc-sysfs-devtmpfs-post-transition-identity",
                "bounded-canonical-json-process-decode",
                "absolute-argv0-numeric-explicit-user-group-admission",
                "private-mount-device-sysfs-cgroup-lifecycle-authority-boundary",
                "empty-capability-sets-securebits-no-new-privs-seccomp",
                "fork-execve-cloexec-launch-status",
                "signalfd-process-group-forwarding-wait4-reaping",
            ],
            "workload_started": True,
        }
    )


def _stage1_abi_bytes() -> bytes:
    consumer = _guest_consumer_contract_bytes()
    return canonical_json_bytes(
        {
            "capability": OCI_BOOTSTRAP_CAPABILITY,
            "consumer_contract": OCI_GUEST_STAGE1_CONTRACT,
            "consumer_contract_digest": _digest(consumer),
            "embedded_consumer": True,
            "plan_transport": OCI_STAGE1_PLAN_TRANSPORT,
            "pivot_root": False,
            "root_assembly": True,
            "root_is_slash": True,
            "root_transition": {
                "contract": OCI_STAGE1_ROOT_TRANSITION_CONTRACT,
                "method": "move-mount-chroot",
                "pid1_root_matches_slash": True,
                "pivot_root": False,
                "pseudo_filesystems": ["dev", "sys", "proc"],
                "root_filesystem": "overlay",
                "switch_root": True,
                "workload_started": False,
            },
            "schema": OCI_STAGE1_ABI,
            "supervisor": _supervisor_contract_dict(),
            "switch_root": True,
            "workload_started": True,
        }
    )


@dataclass(frozen=True, slots=True)
class InitramfsEntryReceipt:
    path: str
    mode: int
    size_bytes: int
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _canonical_path(self.path))
        if type(self.mode) is not int or self.mode not in {
            stat.S_IFDIR | 0o755,
            stat.S_IFREG | 0o644,
            stat.S_IFREG | 0o755,
        }:
            raise ArtifactValidationError("initramfs receipt mode is invalid")
        if type(self.size_bytes) is not int or not 0 <= self.size_bytes <= MAX_OCI_INITRAMFS_ENTRY_BYTES:
            raise ArtifactValidationError("initramfs receipt size is invalid")
        object.__setattr__(self, "digest", _canonical_digest(self.digest, "initramfs entry digest"))

    def to_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "mode": self.mode, "path": self.path, "size_bytes": self.size_bytes}

    @classmethod
    def from_dict(cls, value: Any) -> InitramfsEntryReceipt:
        if not isinstance(value, Mapping) or set(value) != {"digest", "mode", "path", "size_bytes"}:
            raise ArtifactValidationError("initramfs entry receipt fields are invalid")
        receipt = cls(value["path"], value["mode"], value["size_bytes"], value["digest"])
        if receipt.to_dict() != dict(value):
            raise ArtifactValidationError("initramfs entry receipt is not canonical")
        return receipt


@dataclass(frozen=True, slots=True)
class OCIInitramfsManifest:
    artifact_digest: str
    artifact_size_bytes: int
    entries: tuple[InitramfsEntryReceipt, ...]
    stage1_binary_digest: str
    stage1_abi_digest: str
    consumer_contract_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_digest", _canonical_digest(self.artifact_digest, "initramfs digest"))
        object.__setattr__(
            self,
            "stage1_binary_digest",
            _canonical_digest(self.stage1_binary_digest, "stage-1 binary digest"),
        )
        object.__setattr__(self, "stage1_abi_digest", _canonical_digest(self.stage1_abi_digest, "stage-1 ABI digest"))
        object.__setattr__(
            self,
            "consumer_contract_digest",
            _canonical_digest(self.consumer_contract_digest, "guest-consumer contract digest"),
        )
        if type(self.artifact_size_bytes) is not int or not 1 <= self.artifact_size_bytes <= MAX_OCI_INITRAMFS_BYTES:
            raise ArtifactValidationError("initramfs size is invalid")
        if (
            not isinstance(self.entries, tuple)
            or any(not isinstance(receipt, InitramfsEntryReceipt) for receipt in self.entries)
            or tuple((receipt.path, receipt.mode) for receipt in self.entries) != _REQUIRED_LAYOUT
        ):
            raise ArtifactValidationError("initramfs manifest entries are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "architecture": "x86_64",
            "archive": {
                "digest": self.artifact_digest,
                "format": "newc",
                "size_bytes": self.artifact_size_bytes,
            },
            "entries": [entry.to_dict() for entry in self.entries],
            "generator": OCI_INITRAMFS_GENERATOR_CONTRACT,
            "schema": OCI_INITRAMFS_MANIFEST_SCHEMA,
            "stage1": {
                "abi": OCI_STAGE1_ABI,
                "abi_digest": self.stage1_abi_digest,
                "binary_digest": self.stage1_binary_digest,
                "build": {
                    "contract": OCI_STAGE1_BUILD_CONTRACT,
                    "recipe_digest": OCI_STAGE1_BUILD_RECIPE_DIGEST,
                    "seal_recipe_digest": OCI_STAGE1_SEAL_RECIPE_DIGEST,
                    "source_digest": OCI_STAGE1_SOURCE_DIGEST,
                    "toolchain_image": OCI_STAGE1_TOOLCHAIN_IMAGE,
                },
                "capability": OCI_BOOTSTRAP_CAPABILITY,
                "consumer_contract": OCI_GUEST_STAGE1_CONTRACT,
                "consumer_contract_digest": self.consumer_contract_digest,
                "contract": OCI_BOOTSTRAP_STAGE1_CONTRACT,
                "embedded_consumer": True,
                "linkage": "static",
                "plan_transport": OCI_STAGE1_PLAN_TRANSPORT,
                "pivot_root": False,
                "root_assembly": True,
                "root_is_slash": True,
                "root_transition": {
                    "contract": OCI_STAGE1_ROOT_TRANSITION_CONTRACT,
                    "method": "move-mount-chroot",
                    "pid1_root_matches_slash": True,
                    "pivot_root": False,
                    "pseudo_filesystems": ["dev", "sys", "proc"],
                    "root_filesystem": "overlay",
                    "switch_root": True,
                    "workload_started": False,
                },
                "supervisor": _supervisor_contract_dict(),
                "switch_root": True,
                "workload_started": True,
            },
        }

    @property
    def digest(self) -> str:
        return _digest(canonical_json_bytes(self.to_dict()))

    @classmethod
    def from_dict(cls, value: Any) -> OCIInitramfsManifest:
        if not isinstance(value, Mapping) or set(value) != {
            "architecture",
            "archive",
            "entries",
            "generator",
            "schema",
            "stage1",
        }:
            raise ArtifactValidationError("initramfs manifest fields are invalid")
        archive = value.get("archive")
        stage1 = value.get("stage1")
        entries = value.get("entries")
        if (
            value.get("schema") != OCI_INITRAMFS_MANIFEST_SCHEMA
            or value.get("generator") != OCI_INITRAMFS_GENERATOR_CONTRACT
            or value.get("architecture") != "x86_64"
            or not isinstance(archive, Mapping)
            or set(archive) != {"digest", "format", "size_bytes"}
            or archive.get("format") != "newc"
            or not isinstance(stage1, Mapping)
            or stage1
            != {
                "abi": OCI_STAGE1_ABI,
                "abi_digest": stage1.get("abi_digest"),
                "binary_digest": stage1.get("binary_digest"),
                "build": {
                    "contract": OCI_STAGE1_BUILD_CONTRACT,
                    "recipe_digest": OCI_STAGE1_BUILD_RECIPE_DIGEST,
                    "seal_recipe_digest": OCI_STAGE1_SEAL_RECIPE_DIGEST,
                    "source_digest": OCI_STAGE1_SOURCE_DIGEST,
                    "toolchain_image": OCI_STAGE1_TOOLCHAIN_IMAGE,
                },
                "capability": OCI_BOOTSTRAP_CAPABILITY,
                "consumer_contract": OCI_GUEST_STAGE1_CONTRACT,
                "consumer_contract_digest": stage1.get("consumer_contract_digest"),
                "contract": OCI_BOOTSTRAP_STAGE1_CONTRACT,
                "embedded_consumer": True,
                "linkage": "static",
                "plan_transport": OCI_STAGE1_PLAN_TRANSPORT,
                "pivot_root": False,
                "root_assembly": True,
                "root_is_slash": True,
                "root_transition": {
                    "contract": OCI_STAGE1_ROOT_TRANSITION_CONTRACT,
                    "method": "move-mount-chroot",
                    "pid1_root_matches_slash": True,
                    "pivot_root": False,
                    "pseudo_filesystems": ["dev", "sys", "proc"],
                    "root_filesystem": "overlay",
                    "switch_root": True,
                    "workload_started": False,
                },
                "supervisor": _supervisor_contract_dict(),
                "switch_root": True,
                "workload_started": True,
            }
            or not isinstance(entries, list)
            or len(entries) != len(_REQUIRED_LAYOUT)
        ):
            raise ArtifactValidationError("initramfs manifest policy is invalid")
        manifest = cls(
            archive["digest"],
            archive["size_bytes"],
            tuple(InitramfsEntryReceipt.from_dict(entry) for entry in entries),
            stage1["binary_digest"],
            stage1["abi_digest"],
            stage1["consumer_contract_digest"],
        )
        if manifest.to_dict() != dict(value):
            raise ArtifactValidationError("initramfs manifest is not canonical")
        return manifest


@dataclass(frozen=True, slots=True)
class BuiltOCIInitramfs:
    payload: bytes
    manifest: OCIInitramfsManifest

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes) or not isinstance(self.manifest, OCIInitramfsManifest):
            raise ArtifactValidationError("built initramfs is invalid")
        verify_bootstrap_initramfs(self.payload, self.manifest)


def _entry_receipt(entry: NewcEntry) -> InitramfsEntryReceipt:
    return InitramfsEntryReceipt(entry.path, entry.mode, len(entry.data), _digest(entry.data))


def build_bootstrap_initramfs() -> BuiltOCIInitramfs:
    """Build the portable first-party fail-closed bootstrap artifact."""

    stage1 = _bootstrap_stage1_binary()
    consumer = _guest_consumer_contract_bytes()
    abi = _stage1_abi_bytes()
    entries = (
        NewcEntry("dev", stat.S_IFDIR | 0o755, b""),
        NewcEntry("etc", stat.S_IFDIR | 0o755, b""),
        NewcEntry("etc/palimpsest", stat.S_IFDIR | 0o755, b""),
        NewcEntry("etc/palimpsest/guest-stage1-consumer.json", stat.S_IFREG | 0o644, consumer),
        NewcEntry("etc/palimpsest/stage1-abi.json", stat.S_IFREG | 0o644, abi),
        NewcEntry("init", stat.S_IFREG | 0o755, stage1),
        NewcEntry("proc", stat.S_IFDIR | 0o755, b""),
        NewcEntry("run", stat.S_IFDIR | 0o755, b""),
        NewcEntry("run/palimpsest", stat.S_IFDIR | 0o755, b""),
        NewcEntry("run/palimpsest/lowers", stat.S_IFDIR | 0o755, b""),
        NewcEntry("run/palimpsest/merged", stat.S_IFDIR | 0o755, b""),
        NewcEntry("run/palimpsest/root", stat.S_IFDIR | 0o755, b""),
        NewcEntry("sys", stat.S_IFDIR | 0o755, b""),
    )
    payload = build_newc(entries)
    manifest = OCIInitramfsManifest(
        _digest(payload),
        len(payload),
        tuple(_entry_receipt(entry) for entry in entries),
        _digest(stage1),
        _digest(abi),
        _digest(consumer),
    )
    return BuiltOCIInitramfs(payload, manifest)


def verify_bootstrap_initramfs(payload: bytes, manifest: OCIInitramfsManifest) -> tuple[NewcEntry, ...]:
    """Verify exact artifact bytes against the first-party bootstrap policy."""

    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_OCI_INITRAMFS_BYTES:
        raise ArtifactValidationError("initramfs archive bytes are invalid")
    if not isinstance(manifest, OCIInitramfsManifest):
        raise ArtifactValidationError("initramfs manifest is invalid")
    if _digest(payload) != manifest.artifact_digest or len(payload) != manifest.artifact_size_bytes:
        raise ArtifactValidationError("initramfs archive does not match its manifest")
    entries = parse_newc(payload)
    receipts = tuple(_entry_receipt(entry) for entry in entries)
    if receipts != manifest.entries or tuple(entry.path for entry in entries) != _REQUIRED_PATHS:
        raise ArtifactValidationError("initramfs entries do not match the manifest")
    by_path = {entry.path: entry for entry in entries}
    stage1 = by_path["init"].data
    abi = by_path["etc/palimpsest/stage1-abi.json"].data
    consumer = by_path["etc/palimpsest/guest-stage1-consumer.json"].data
    verify_static_x86_64_elf(stage1)
    if (
        stage1 != _bootstrap_stage1_binary()
        or abi != _stage1_abi_bytes()
        or consumer != _guest_consumer_contract_bytes()
        or _digest(stage1) != manifest.stage1_binary_digest
        or _digest(abi) != manifest.stage1_abi_digest
        or _digest(consumer) != manifest.consumer_contract_digest
    ):
        raise ArtifactValidationError("initramfs first-party stage-1 binding is invalid")
    return entries


__all__ = [
    "MAX_OCI_INITRAMFS_BYTES",
    "MAX_OCI_INITRAMFS_ENTRIES",
    "BuiltOCIInitramfs",
    "InitramfsEntryReceipt",
    "NewcEntry",
    "OCIInitramfsManifest",
    "OCI_BOOTSTRAP_CAPABILITY",
    "OCI_BOOTSTRAP_STAGE1_CONTRACT",
    "OCI_INITRAMFS_GENERATOR_CONTRACT",
    "OCI_INITRAMFS_MANIFEST_SCHEMA",
    "OCI_STAGE1_ABI",
    "OCI_STAGE1_BINARY_DIGEST",
    "OCI_STAGE1_BUILD_CONTRACT",
    "OCI_STAGE1_BUILD_RECIPE_DIGEST",
    "OCI_STAGE1_LIFECYCLE_BROKER_CONTRACT",
    "OCI_STAGE1_PLAN_TRANSPORT",
    "OCI_STAGE1_ROOT_TRANSITION_CONTRACT",
    "OCI_STAGE1_SEAL_RECIPE_DIGEST",
    "OCI_STAGE1_SOURCE_DIGEST",
    "OCI_STAGE1_SUPERVISOR_CONTRACT",
    "OCI_STAGE1_WORKLOAD_ISOLATION_CONTRACT",
    "OCI_STAGE1_TOOLCHAIN_IMAGE",
    "build_bootstrap_initramfs",
    "build_newc",
    "parse_newc",
    "verify_bootstrap_initramfs",
    "verify_static_x86_64_elf",
]
