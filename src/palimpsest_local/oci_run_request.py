"""Typed local OCI create input, independent of cloud image stacks.

The first public local-image contract uses the authenticated image process as-is:
no host environment inheritance, shell expansion, or process override rewrites.
Materialization produces immutable cache receipts, not a running VM or leases.
"""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .digest import normalize_digest
from .errors import ArtifactValidationError, UnsupportedPlatformError
from .oci_materializer import OCIImageMaterializationReceipt, materialize_image_hard
from .oci_packer import VerifiedSquashFSToolchain
from .oci_source import LocalArchiveSource, LocalLayoutSource, SourceCAS
from .oci_store import OCIStore
from .project_volumes import _validate_size
from .runtime_types import DispatchKey, RuntimeBackend, RuntimeKind
from .state import StatePaths


@dataclass(frozen=True, slots=True)
class LocalOCIRunRequest:
    """Logical local OCI launch policy; never a cloud ``RunSpec`` substitute."""

    name: str
    source: Path = field(repr=False)
    manifest_digest: str | None = None
    detached: bool = False
    memory_mib: int = 512
    vcpus: int = 1
    root_size_bytes: int = 4 * 1024**3
    network: None = None
    platform: str = "linux/amd64"
    backend: str = "kvm"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", self.name) is None:
            raise ArtifactValidationError("OCI run name is invalid")
        if (
            not isinstance(self.source, Path)
            or not self.source.is_absolute()
            or ".." in self.source.parts
            or "\x00" in str(self.source)
        ):
            raise ArtifactValidationError("local OCI run source must be an absolute path")
        if self.manifest_digest is not None:
            if (
                not isinstance(self.manifest_digest, str)
                or normalize_digest(self.manifest_digest) != self.manifest_digest
            ):
                raise ArtifactValidationError("local OCI run manifest digest must be canonical")
        if type(self.detached) is not bool:
            raise ArtifactValidationError("OCI run detached policy must be a boolean")
        if type(self.memory_mib) is not int or not 256 <= self.memory_mib <= 1_048_576:
            raise ArtifactValidationError("OCI run memory must be between 256 and 1048576 MiB")
        if type(self.vcpus) is not int or not 1 <= self.vcpus <= 256:
            raise ArtifactValidationError("OCI run vcpus must be between 1 and 256")
        _validate_size(self.root_size_bytes)
        if self.network is not None:
            raise ArtifactValidationError("local OCI run networking is not available yet")
        if self.platform != "linux/amd64" or self.backend != "kvm":
            raise ArtifactValidationError("local OCI run supports only linux/amd64 on KVM")

    @property
    def dispatch_key(self) -> DispatchKey:
        return DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)


@dataclass(frozen=True, slots=True)
class PreparedLocalOCIRun:
    """Authenticated image input ready for the separate host launch boundary."""

    request: LocalOCIRunRequest
    receipt: OCIImageMaterializationReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.request, LocalOCIRunRequest) or not isinstance(
            self.receipt, OCIImageMaterializationReceipt
        ):
            raise ArtifactValidationError("prepared local OCI run requires a typed request and image receipt")
        if (
            self.request.manifest_digest is not None
            and self.request.manifest_digest != self.receipt.root_descriptor.digest
        ):
            raise ArtifactValidationError("prepared local OCI run does not match its root pin")
        self.receipt.process.require_bootable()


def resolve_local_oci_run_request(
    source: Path,
    *,
    name: str,
    manifest_digest: str | None = None,
    detached: bool = False,
    memory_mib: int = 512,
    vcpus: int = 1,
    root_size_bytes: int = 4 * 1024**3,
    network: None = None,
    platform: str = "linux/amd64",
    backend: str = "kvm",
) -> LocalOCIRunRequest:
    """Resolve only a local path; image selection happens in the secure snapshot."""
    if not isinstance(source, Path):
        raise ArtifactValidationError("local OCI run source must be a path")
    try:
        selected = source.expanduser().resolve(strict=True)
        if not selected.is_file() and not selected.is_dir():
            raise OSError("unsupported source type")
    except (OSError, RuntimeError, ValueError):
        raise ArtifactValidationError("local OCI run source is missing or not a regular file/directory") from None
    return LocalOCIRunRequest(
        name=name,
        source=selected,
        manifest_digest=manifest_digest,
        detached=detached,
        memory_mib=memory_mib,
        vcpus=vcpus,
        root_size_bytes=root_size_bytes,
        network=network,
        platform=platform,
        backend=backend,
    )


def materialize_local_oci_run(
    request: LocalOCIRunRequest,
    *,
    roots: StatePaths,
    packer_path: Path,
    toolchain: VerifiedSquashFSToolchain,
    timeout_seconds: float = 300.0,
) -> PreparedLocalOCIRun:
    """Snapshot the unique/pinned local root and use the existing hard worker.

    No VM state is created here. An unbootable image is rejected before layer
    conversion, and a failed conversion retains only retryable immutable cache.
    """
    if not isinstance(request, LocalOCIRunRequest) or not isinstance(roots, StatePaths):
        raise ArtifactValidationError("local OCI intake requires a typed request and state paths")
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise UnsupportedPlatformError("local OCI run materialization requires Linux")
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ArtifactValidationError("local OCI materialization timeout is invalid")
    source = (
        LocalLayoutSource(request.source, request.manifest_digest)
        if request.source.is_dir()
        else LocalArchiveSource(request.source, request.manifest_digest)
    )
    image = source.snapshot(None, SourceCAS(roots.oci_source_cas))
    image.image.config.process.require_bootable()
    receipt = materialize_image_hard(
        image,
        source_cas_root=roots.oci_source_cas,
        roots=roots,
        store=OCIStore(roots),
        packer_path=packer_path,
        toolchain=toolchain,
        timeout_seconds=timeout_seconds,
    )
    return PreparedLocalOCIRun(request, receipt)
