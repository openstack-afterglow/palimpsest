"""Frozen, verified public artifact and runtime references."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .digest import require_digest, require_file_digest
from .errors import ArtifactValidationError

_DOMAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_LOGICAL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class PortForward:
    host_ip: str
    host_port: int
    guest_port: int
    protocol: Literal["tcp", "udp"] = "tcp"

    def __post_init__(self) -> None:
        try:
            ipaddress.ip_address(self.host_ip)
        except ValueError as exc:
            raise ArtifactValidationError(f"invalid port-forward host IP: {self.host_ip!r}") from exc
        if not 1 <= self.host_port <= 65535 or not 1 <= self.guest_port <= 65535:
            raise ArtifactValidationError("forwarded ports must be between 1 and 65535")
        if self.protocol not in {"tcp", "udp"}:
            raise ArtifactValidationError("port-forward protocol must be tcp or udp")


@dataclass(frozen=True)
class VolumeAttachment:
    name: str
    mount_path: str
    host_path: Path | None = None
    backend_name: str | None = None
    filesystem: Literal["ext4"] = "ext4"
    read_only: bool = False
    format: bool = False

    def __post_init__(self) -> None:
        if _LOGICAL_NAME_RE.fullmatch(self.name) is None:
            raise ArtifactValidationError("volume names must match ^[a-z0-9][a-z0-9_.-]{0,62}$")
        target = PurePosixPath(self.mount_path)
        if not target.is_absolute() or ".." in target.parts or str(target) == "/":
            raise ArtifactValidationError("volume mount paths must be normalized absolute guest paths below /")
        if any(str(target) == prefix or str(target).startswith(prefix + "/") for prefix in ("/dev", "/proc", "/sys")):
            raise ArtifactValidationError("volume mount paths cannot shadow /dev, /proc, or /sys")
        if (self.host_path is None) == (self.backend_name is None):
            raise ArtifactValidationError("a volume attachment requires exactly one host_path or backend_name")
        if self.host_path is not None:
            path = self.host_path.resolve()
            if not path.is_file():
                raise ArtifactValidationError(f"volume block artifact is not a file: {path}")
            object.__setattr__(self, "host_path", path)
        if self.backend_name is not None and _DOMAIN_NAME_RE.fullmatch(self.backend_name) is None:
            raise ArtifactValidationError("volume backend_name must match ^[a-z0-9][a-z0-9-]{0,62}$")
        if not isinstance(self.format, bool):
            raise ArtifactValidationError("volume format policy must be a boolean")
        if self.format and self.backend_name is None:
            raise ArtifactValidationError("only Lima backend volumes can request first-use formatting")
        if self.filesystem != "ext4":
            raise ArtifactValidationError("only ext4 writable block volumes are supported")


@dataclass(frozen=True)
class HostDirectoryShare:
    """Project-contained directory policy; live sources are validated explicitly."""

    project_root: Path
    source: str
    mount_path: str
    read_only: bool = False

    def __post_init__(self) -> None:
        from .project import ProjectError, _guest_path, _reserved_guest_path

        if (
            not isinstance(self.project_root, Path)
            or not self.project_root.is_absolute()
            or ".." in self.project_root.parts
            or str(self.project_root).startswith("//")
            or any(character in str(self.project_root) for character in ("\\", "\x00", "\n", "\r"))
        ):
            raise ArtifactValidationError("host share project root must be a normalized absolute path")
        if not isinstance(self.source, str):
            raise ArtifactValidationError("host share source must be a normalized project-relative directory")
        relative = PurePosixPath(self.source)
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or str(relative) != self.source
            or "\\" in self.source
            or any(character in self.source for character in ("\x00", "\n", "\r"))
        ):
            raise ArtifactValidationError("host share source must be a normalized project-relative directory")
        try:
            str(self.project_root).encode("utf-8")
            self.source.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ArtifactValidationError("host share paths must contain valid UTF-8 text") from exc
        if not isinstance(self.mount_path, str) or any(
            character in self.mount_path for character in ("\x00", "\n", "\r")
        ):
            raise ArtifactValidationError("host share target must be a single-line guest path")
        try:
            canonical_target = _guest_path(self.mount_path, "host share target")
        except ProjectError as exc:
            raise ArtifactValidationError(str(exc)) from exc
        if canonical_target != self.mount_path:
            raise ArtifactValidationError("host share target must be a normalized absolute guest path")
        if _reserved_guest_path(self.mount_path):
            raise ArtifactValidationError("host share target overlaps a runtime-owned guest path")
        if type(self.read_only) is not bool:
            raise ArtifactValidationError("host share read_only must be a boolean")

    @property
    def host_path(self) -> Path:
        return self.project_root / self.source

    @property
    def guest_tag(self) -> str:
        identity = f"palimpsest-virtiofs-v1:{self.host_path}:{self.mount_path}"
        return "ps-" + hashlib.sha256(identity.encode()).hexdigest()[:24]

    def validate_source(self) -> None:
        """Validate at launch/live retention, never while decoding cleanup policy."""
        current = Path(self.host_path.anchor)
        try:
            for part in self.host_path.parts[1:]:
                current /= part
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise ArtifactValidationError("host share source cannot traverse a symlink")
            self.host_path.resolve(strict=True).relative_to(self.project_root.resolve(strict=True))
            if not stat.S_ISDIR(metadata.st_mode):
                raise ArtifactValidationError(
                    "virtiofs requires a host directory; host file binds are unsupported "
                    "(share the containing directory instead)"
                )
        except (OSError, ValueError) as exc:
            raise ArtifactValidationError("host share source is missing or escapes the project root") from exc


@dataclass(frozen=True)
class ImageRef:
    digest: str
    disk_format: Literal["qcow2", "raw"]
    arch: Literal["x86_64", "aarch64"]
    os_variant: str | None
    local_path: Path

    def __post_init__(self) -> None:
        if self.disk_format not in {"qcow2", "raw"}:
            raise ArtifactValidationError(f"unsupported image disk format: {self.disk_format!r}")
        if self.arch not in {"x86_64", "aarch64"}:
            raise ArtifactValidationError(f"unsupported image architecture: {self.arch!r}")
        object.__setattr__(self, "digest", require_digest(self.digest))
        path = self.local_path.resolve()
        if not path.is_file():
            raise ArtifactValidationError(f"image artifact is not a file: {path}")
        require_file_digest(path, self.digest)
        object.__setattr__(self, "local_path", path)


@dataclass(frozen=True)
class LayerRef:
    digest: str
    media_type: str
    local_path: Path

    def __post_init__(self) -> None:
        if not self.media_type:
            raise ArtifactValidationError("layer media type must be nonempty")
        object.__setattr__(self, "digest", require_digest(self.digest))
        path = self.local_path.resolve()
        if not path.is_file():
            raise ArtifactValidationError(f"layer artifact is not a file: {path}")
        require_file_digest(path, self.digest)
        object.__setattr__(self, "local_path", path)


@dataclass(frozen=True)
class StackRef:
    base: ImageRef
    layers: tuple[LayerRef, ...]

    def __post_init__(self) -> None:
        if len(self.layers) > 25:
            raise ArtifactValidationError("a stack supports at most 25 layer disks")
        digests = [layer.digest for layer in self.layers]
        if len(set(digests)) != len(digests):
            raise ArtifactValidationError("a stack cannot attach duplicate layer digests")


@dataclass(frozen=True)
class RunSpec:
    name: str
    stack: StackRef
    memory_mib: int = 4096
    vcpus: int = 2
    network: str = "default"
    writable_overlay: Path | None = None
    seed: Path | None = None
    ports: tuple[PortForward, ...] = ()
    volumes: tuple[VolumeAttachment, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    cloud_init: object | None = None
    host_shares: tuple[HostDirectoryShare, ...] = ()

    def __post_init__(self) -> None:
        if _DOMAIN_NAME_RE.fullmatch(self.name) is None:
            raise ArtifactValidationError("run names must match ^[a-z0-9][a-z0-9-]{0,62}$")
        if not 256 <= self.memory_mib <= 1_048_576:
            raise ArtifactValidationError("memory must be between 256 and 1048576 MiB")
        if not 1 <= self.vcpus <= 256:
            raise ArtifactValidationError("vcpus must be between 1 and 256")
        if not self.network:
            raise ArtifactValidationError("network must be nonempty")
        port_keys = [(item.host_ip, item.host_port, item.protocol) for item in self.ports]
        if len(set(port_keys)) != len(port_keys):
            raise ArtifactValidationError("run cannot contain duplicate host port bindings")
        volume_names = [item.name for item in self.volumes]
        volume_targets = [item.mount_path for item in self.volumes]
        if not isinstance(self.host_shares, tuple) or not all(
            isinstance(share, HostDirectoryShare) for share in self.host_shares
        ):
            raise ArtifactValidationError("host shares must be immutable directory share policies")
        volume_targets.extend(share.mount_path for share in self.host_shares)
        tags = [share.guest_tag for share in self.host_shares]
        if len(set(tags)) != len(tags):
            raise ArtifactValidationError("run cannot attach duplicate host share tags")
        if len(set(volume_names)) != len(volume_names) or len(set(volume_targets)) != len(volume_targets):
            raise ArtifactValidationError("run cannot attach duplicate volume names or mount paths")
        environment_names: set[str] = set()
        for key, value in self.environment:
            if _ENVIRONMENT_NAME_RE.fullmatch(key) is None:
                raise ArtifactValidationError(f"invalid environment variable name: {key!r}")
            if key in environment_names:
                raise ArtifactValidationError(f"duplicate environment variable: {key}")
            if any(character in value for character in ("\x00", "\n", "\r")):
                raise ArtifactValidationError(f"environment variable {key} must be a single NUL-free line")
            environment_names.add(key)


@dataclass(frozen=True)
class BuildSpec:
    base: ImageRef
    parent_layers: tuple[LayerRef, ...]
    recipe: Path
    network: Literal["none", "default"] = "none"
    output_name: str = "layer"

    def __post_init__(self) -> None:
        if self.network not in {"none", "default"}:
            raise ArtifactValidationError(f"unsupported build network: {self.network!r}")
        recipe = self.recipe.resolve()
        if not recipe.is_file():
            raise ArtifactValidationError(f"recipe is not a file: {recipe}")
        if len(self.parent_layers) > 25:
            raise ArtifactValidationError("a build supports at most 25 parent layers")
        object.__setattr__(self, "recipe", recipe)
