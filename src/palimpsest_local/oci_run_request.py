"""Typed local OCI create input, independent of cloud image stacks.

The local-image contract uses the authenticated image process as-is except for an
explicit user-only override: there is no host environment inheritance, shell
expansion, or other process rewriting. Materialization receipts remain immutable
and describe the original image process, not a running VM or leases.
"""

from __future__ import annotations

import math
import os
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .digest import normalize_digest
from .errors import ArtifactValidationError, UnsupportedPlatformError
from .oci_materializer import OCIImageMaterializationReceipt, materialize_image_hard
from .oci_network import OCI_NETWORK_DEFAULT, OCINetworkConfig
from .oci_packer import VerifiedSquashFSToolchain
from .oci_process import OCIProcessSpec, OCIUserSpec
from .oci_source import LocalArchiveSource, LocalLayoutSource, SourceCAS, SourceSnapshot
from .oci_store import OCIStore
from .project_volumes import _validate_size
from .runtime_types import DispatchKey, ExperimentalOCIDispatchKey, RuntimeBackend, RuntimeKind, RunVolumeIntent
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
    root_retention: str = "delete"
    root_volume_id: str | None = None
    network: OCINetworkConfig = OCI_NETWORK_DEFAULT
    platform: str = "linux/amd64"
    backend: str = "kvm"
    user_override: OCIUserSpec | None = None
    command_override: tuple[str, ...] | None = None
    force_architecture: bool = False
    vm_image: Path | None = field(default=None, repr=False)
    vm_image_digest: str | None = None
    volume_intents: tuple[RunVolumeIntent, ...] = ()

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
        if self.user_override is not None and not isinstance(self.user_override, OCIUserSpec):
            raise ArtifactValidationError("local OCI run user override must be typed")
        if self.command_override is not None:
            if not isinstance(self.command_override, tuple) or not self.command_override:
                raise ArtifactValidationError("local OCI run command override must be a nonempty tuple")
            # Reuse the canonical process aggregate/string validation.
            OCIProcessSpec(self.command_override, (), "/", OCIUserSpec("0", "0"), 15)
        if type(self.detached) is not bool:
            raise ArtifactValidationError("OCI run detached policy must be a boolean")
        if type(self.memory_mib) is not int or not 256 <= self.memory_mib <= 1_048_576:
            raise ArtifactValidationError("OCI run memory must be between 256 and 1048576 MiB")
        if type(self.vcpus) is not int or not 1 <= self.vcpus <= 256:
            raise ArtifactValidationError("OCI run vcpus must be between 1 and 256")
        _validate_size(self.root_size_bytes)
        if not isinstance(self.volume_intents, tuple) or not all(
            isinstance(intent, RunVolumeIntent) for intent in self.volume_intents
        ):
            raise ArtifactValidationError("OCI run requires immutable typed volume intents")
        if self.volume_intents:
            if self.backend == "lima-qemu-oci":
                raise ArtifactValidationError(
                    "data volume attachment to the experimental OCI carrier is unsupported; "
                    "carrier container disks are not attached; use a cloud-image VM or compose block service"
                )
            raise ArtifactValidationError(
                "data volume attachment to native OCI-root is unsupported; use cloud-image VM or compose block service"
            )
        if type(self.root_retention) is not str or self.root_retention not in {"delete", "retain"}:
            raise ArtifactValidationError("OCI run root retention policy is invalid")
        if self.root_volume_id is not None:
            try:
                parsed_root_volume_id = uuid.UUID(self.root_volume_id)
            except (AttributeError, TypeError, ValueError):
                raise ArtifactValidationError("OCI run root volume ID must be a canonical UUID") from None
            if str(parsed_root_volume_id) != self.root_volume_id:
                raise ArtifactValidationError("OCI run root volume ID must be a canonical UUID")
            if self.root_retention != "retain":
                raise ArtifactValidationError("OCI run retained root reuse requires --root-retention retain")
        if not isinstance(self.network, OCINetworkConfig):
            raise ArtifactValidationError(
                "OCI run networking must be a typed configuration; the CLI resolves --network and --publish"
            )
        if type(self.force_architecture) is not bool:
            raise ArtifactValidationError("force architecture policy must be a boolean")
        if self.platform not in {"linux/amd64", "linux/arm64"}:
            raise ArtifactValidationError("local OCI platform must be linux/amd64 or linux/arm64")
        if self.backend == "lima-qemu-oci":
            if not self.force_architecture:
                raise ArtifactValidationError("experimental OCI execution requires --force-architecture")
            if self.detached:
                raise ArtifactValidationError("experimental Lima/QEMU OCI execution is foreground-only; omit --detach")
            if self.root_retention != "delete" or self.root_volume_id is not None:
                raise ArtifactValidationError(
                    "experimental OCI execution has a disposable VM disk, not native retained roots"
                )
            if self.network.mode == "host-only":
                raise ArtifactValidationError("experimental OCI execution supports nat or none, not host-only")
            if any(port.protocol != "tcp" or port.host_ip != "127.0.0.1" for port in self.network.published_ports):
                raise ArtifactValidationError("experimental OCI publications require TCP on 127.0.0.1")
        elif self.platform != "linux/amd64" or self.backend != "kvm" or self.force_architecture:
            raise ArtifactValidationError("native local OCI run supports only linux/amd64 on KVM")
        elif self.vm_image is not None or self.vm_image_digest is not None:
            raise ArtifactValidationError("--vm-image and --vm-image-digest require --force-architecture")
        if self.vm_image is not None and (not isinstance(self.vm_image, Path) or not self.vm_image.is_absolute()):
            raise ArtifactValidationError("experimental VM carrier must be an absolute local path")
        if self.vm_image_digest is not None and (
            not isinstance(self.vm_image_digest, str) or normalize_digest(self.vm_image_digest) != self.vm_image_digest
        ):
            raise ArtifactValidationError("experimental VM carrier digest must be canonical sha256")

    @property
    def dispatch_key(self) -> DispatchKey | ExperimentalOCIDispatchKey:
        if self.backend == "lima-qemu-oci":
            return ExperimentalOCIDispatchKey()
        return DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)


@dataclass(frozen=True, slots=True)
class PreparedLocalOCIRun:
    """Authenticated image input ready for the separate host launch boundary."""

    request: LocalOCIRunRequest
    receipt: OCIImageMaterializationReceipt
    source_cas: SourceCAS
    config_snapshot: SourceSnapshot

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
        if not isinstance(self.source_cas, SourceCAS) or not isinstance(self.config_snapshot, SourceSnapshot):
            raise ArtifactValidationError("prepared local OCI run config authority is invalid")
        if self.config_snapshot.descriptor != self.receipt.config_descriptor:
            raise ArtifactValidationError("prepared local OCI run config descriptor is invalid")
        self.receipt.process.require_bootable()


def resolve_local_oci_run_request(
    source: Path,
    *,
    name: str,
    manifest_digest: str | None = None,
    user_override: OCIUserSpec | None = None,
    command_override: tuple[str, ...] | None = None,
    detached: bool = False,
    memory_mib: int = 512,
    vcpus: int = 1,
    root_size_bytes: int = 4 * 1024**3,
    root_retention: str = "delete",
    root_volume_id: str | None = None,
    network: str | OCINetworkConfig | None = None,
    published_ports: Sequence[str] = (),
    platform: str = "linux/amd64",
    backend: str = "auto",
    force_architecture: bool = False,
    vm_image: Path | None = None,
    vm_image_digest: str | None = None,
    volume_intents: tuple[RunVolumeIntent, ...] = (),
) -> LocalOCIRunRequest:
    """Resolve only a local path; image selection happens in the secure snapshot."""
    if not isinstance(source, Path):
        raise ArtifactValidationError("local OCI run source must be a path")
    if vm_image is not None and not isinstance(vm_image, Path):
        raise ArtifactValidationError("local experimental VM carrier must be a path")
    try:
        selected = source.expanduser().resolve(strict=True)
        if not selected.is_file() and not selected.is_dir():
            raise OSError("unsupported source type")
        selected_carrier = vm_image.expanduser().resolve(strict=True) if vm_image is not None else None
    except (OSError, RuntimeError, ValueError):
        raise ArtifactValidationError(
            "local OCI source or VM carrier is missing or not a regular file/directory"
        ) from None
    from .platforms import select_backend

    # Native resolution remains host-independent; its existing adapter owns
    # strict preflight. Only explicit experimental consent adds host selection.
    selected_backend = "kvm" if backend == "auto" else backend
    if force_architecture or backend == "lima-qemu-oci":
        selected_backend = select_backend(
            platform.removeprefix("linux/"),
            requested=backend,
            runtime_kind="oci-root",
            force_architecture=force_architecture,
        )
    return LocalOCIRunRequest(
        name=name,
        source=selected,
        manifest_digest=manifest_digest,
        user_override=user_override,
        command_override=command_override,
        detached=detached,
        memory_mib=memory_mib,
        vcpus=vcpus,
        root_size_bytes=root_size_bytes,
        root_retention=root_retention,
        root_volume_id=root_volume_id,
        network=(
            network if isinstance(network, OCINetworkConfig) else OCINetworkConfig.resolve(network, published_ports)
        ),
        platform=platform,
        backend=selected_backend,
        force_architecture=force_architecture,
        vm_image=selected_carrier,
        vm_image_digest=vm_image_digest,
        volume_intents=volume_intents,
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
    if request.backend != "kvm" or request.platform != "linux/amd64" or request.force_architecture:
        raise ArtifactValidationError("native OCI materialization does not accept experimental VM execution requests")
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise UnsupportedPlatformError("local OCI run materialization requires Linux")
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ArtifactValidationError("local OCI materialization timeout is invalid")
    source = (
        LocalLayoutSource(request.source, request.manifest_digest)
        if request.source.is_dir()
        else LocalArchiveSource(request.source, request.manifest_digest)
    )
    source_cas = SourceCAS(roots.oci_source_cas)
    image = source.snapshot(None, source_cas)
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
    return PreparedLocalOCIRun(request, receipt, source_cas, image.config)
