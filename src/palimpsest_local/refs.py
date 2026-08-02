"""Frozen, verified public artifact and runtime references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .digest import require_digest, require_file_digest
from .errors import ArtifactValidationError

_DOMAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


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

    def __post_init__(self) -> None:
        if _DOMAIN_NAME_RE.fullmatch(self.name) is None:
            raise ArtifactValidationError("run names must match ^[a-z0-9][a-z0-9-]{0,62}$")
        if not 256 <= self.memory_mib <= 1_048_576:
            raise ArtifactValidationError("memory must be between 256 and 1048576 MiB")
        if not 1 <= self.vcpus <= 256:
            raise ArtifactValidationError("vcpus must be between 1 and 256")
        if not self.network:
            raise ArtifactValidationError("network must be nonempty")


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
