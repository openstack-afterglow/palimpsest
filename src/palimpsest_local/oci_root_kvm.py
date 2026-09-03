"""Path-free KVM boot contracts for prepared OCI-root resources.

This module stops at a validated, ephemeral libvirt XML preview.  That preview
is not launch authorization: a future define/start consumer must resolve and
revalidate every path immediately at its own mutation boundary.  The stage-1
initramfs and lifecycle that will actually assemble and pivot to ``/`` are
deliberately not enabled here.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .digest import normalize_digest
from .errors import ArtifactValidationError, StateError
from .kvm import (
    MAX_OCI_ROOT_LAYER_DISKS,
    KvmError,
    LayerDisk,
    OCIRootDomainSpec,
    Stage1TransportDisk,
    build_oci_root_domain_xml,
)
from .oci_control_protocol import OCI_CONTROL_CHANNEL_NAME, OCI_CONTROL_PROTOCOL
from .oci_initramfs import MAX_OCI_INITRAMFS_BYTES, OCIInitramfsManifest, verify_bootstrap_initramfs
from .oci_process import OCIProcessSpec
from .oci_provenance import canonical_json_bytes
from .oci_root_prepare import OCIRootPreparationTransaction, PreparedOCIRootRun
from .oci_root_volume import MAX_OCI_ROOT_VOLUME_GENERATION, load_oci_root_volume
from .oci_stage1 import OCIStage1Plan, oci_stage1_device_serial
from .oci_stage1_transport import (
    OCI_STAGE1_TRANSPORT_FILENAME,
    OCIStage1TransportReceipt,
    build_stage1_transport,
    verify_stage1_transport_file,
)
from .oci_store import OCIStore
from .platforms import DomainProfile
from .project_volumes import CommandRunner, _default_runner
from .runtime_types import RuntimeBackend, RuntimeKind
from .state import StatePaths, locked_existing_run, read_run_ledger_snapshot, run_paths

OCI_ROOT_DOMAIN_PLAN_SCHEMA = "palimpsest.oci-root-domain-plan.v6"
OCI_ROOT_DOMAIN_CORE_SCHEMA = "palimpsest.oci-root-domain-core.v3"
OCI_ROOT_BOOT_ARTIFACT_POLICY = "palimpsest.host-boot-artifacts.x86_64.v1"
_MAX_KERNEL_BYTES = 256 * 1024 * 1024
_MAX_INITRAMFS_BYTES = 1024 * 1024 * 1024
_SERIAL_RE = re.compile(r"^[0-9a-f]{20}$")
_TRANSPORT_RECEIPT_FIELDS = frozenset(
    {
        "artifact_digest",
        "artifact_size_bytes",
        "device_policy",
        "format",
        "payload_digest",
        "payload_size_bytes",
        "schema",
    }
)


def _canonical_digest(value: Any, message: str) -> str:
    if not isinstance(value, str):
        raise StateError(message)
    try:
        normalized = normalize_digest(value)
    except (ArtifactValidationError, TypeError, ValueError):
        raise StateError(message) from None
    if normalized != value:
        raise StateError(message)
    return normalized


def _json_digest(value: Any, message: str) -> str:
    try:
        return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"
    except ArtifactValidationError:
        raise StateError(message) from None


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _frozen_json_object(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            key: _frozen_json_object(item)
            if isinstance(item, Mapping)
            else tuple(_frozen_json_object(child) if isinstance(child, Mapping) else child for child in item)
            if isinstance(item, (list, tuple))
            else item
            for key, item in value.items()
        }
    )


def _serial(namespace: str, identity: str) -> str:
    return oci_stage1_device_serial(namespace, identity)


def _domain_core_dict(
    *,
    run_id: str,
    run_name: str,
    resource_plan_digest: str,
    lower_lease_set_id: str,
    lower_graph_digest: str,
    boot_artifacts: Mapping[str, Any],
    root_volume: Mapping[str, Any],
    layers: tuple[Mapping[str, Any], ...],
    process: OCIProcessSpec,
    memory_mib: int,
    vcpus: int,
    network: str | None,
) -> dict[str, Any]:
    return {
        "boot_artifacts": _plain_json(boot_artifacts),
        "layers": _plain_json(layers),
        "lower_graph_digest": lower_graph_digest,
        "lower_lease_set_id": lower_lease_set_id,
        "lifecycle_control": {
            "channel_name": OCI_CONTROL_CHANNEL_NAME,
            "protocol": OCI_CONTROL_PROTOCOL,
            "transport": "virtio-serial",
        },
        "machine": {"memory_mib": memory_mib, "network": network, "vcpus": vcpus},
        "process": process.to_dict(),
        "resource_plan_digest": resource_plan_digest,
        "root_volume": _plain_json(root_volume),
        "run": {"backend": "kvm", "name": run_name, "run_id": run_id, "runtime_kind": "oci-root"},
        "schema": OCI_ROOT_DOMAIN_CORE_SCHEMA,
    }


@dataclass(frozen=True, slots=True)
class VerifiedHostBootArtifact:
    kind: str
    path: Path
    digest: str
    size_bytes: int
    device: int
    inode: int

    def __post_init__(self) -> None:
        if self.kind not in {"kernel", "initramfs"} or not self.path.is_absolute():
            raise StateError("host boot artifact identity is invalid")
        _canonical_digest(self.digest, "host boot artifact digest is invalid")
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise StateError("host boot artifact size is invalid")
        if type(self.device) is not int or type(self.inode) is not int or self.device < 0 or self.inode <= 0:
            raise StateError("host boot artifact file identity is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "size_bytes": self.size_bytes}


@dataclass(frozen=True, slots=True)
class VerifiedHostBootArtifacts:
    architecture: str
    kernel: VerifiedHostBootArtifact
    initramfs: VerifiedHostBootArtifact
    policy: str = OCI_ROOT_BOOT_ARTIFACT_POLICY

    def __post_init__(self) -> None:
        if (
            self.architecture != "x86_64"
            or self.policy != OCI_ROOT_BOOT_ARTIFACT_POLICY
            or self.kernel.kind != "kernel"
            or self.initramfs.kind != "initramfs"
            or (self.kernel.device, self.kernel.inode) == (self.initramfs.device, self.initramfs.inode)
        ):
            raise StateError("host boot artifact set is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture,
            "initramfs": self.initramfs.to_dict(),
            "kernel": self.kernel.to_dict(),
            "policy": self.policy,
        }


def _verify_host_boot_artifact(
    path: Path,
    *,
    kind: str,
    maximum: int,
    expected_digest: str | None,
    payload_validator: Callable[[bytes], None] | None = None,
) -> VerifiedHostBootArtifact:
    if not isinstance(path, Path) or not path.is_absolute() or "\0" in os.fspath(path):
        raise StateError(f"host {kind} path must be absolute")
    fd: int | None = None
    try:
        visible = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(visible.st_mode):
            raise StateError(f"host {kind} artifact cannot be securely read")
        if not stat.S_ISREG(visible.st_mode):
            raise StateError(f"host {kind} artifact metadata is unsafe")
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(fd)
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISREG(visible.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid not in {0, os.geteuid()}
            or opened.st_nlink != 1
            or mode & 0o022
            or not 1 <= opened.st_size <= maximum
        ):
            raise StateError(f"host {kind} artifact metadata is unsafe")
        prefix = os.pread(fd, min(opened.st_size, 0x206), 0)
        if kind == "kernel":
            if len(prefix) < 0x206 or prefix[0x202:0x206] != b"HdrS":
                raise StateError("host kernel is not an x86 boot-protocol image")
        elif not (
            prefix.startswith(b"\x1f\x8b")
            or prefix.startswith(b"\x28\xb5\x2f\xfd")
            or prefix.startswith(b"\xfd7zXZ\x00")
            or prefix.startswith(b"070701")
            or prefix.startswith(b"\x04\x22\x4d\x18")
        ):
            raise StateError("host initramfs compression or archive format is unsupported")
        hasher = hashlib.sha256()
        collected = bytearray() if payload_validator is not None else None
        offset = 0
        while offset < opened.st_size:
            chunk = os.pread(fd, min(1024 * 1024, opened.st_size - offset), offset)
            if not chunk:
                raise StateError(f"host {kind} artifact ended during verification")
            hasher.update(chunk)
            if collected is not None:
                collected.extend(chunk)
            offset += len(chunk)
        digest = f"sha256:{hasher.hexdigest()}"
        if (
            expected_digest is not None
            and _canonical_digest(expected_digest, f"expected host {kind} digest is invalid") != digest
        ):
            raise StateError(f"host {kind} digest does not match the boot contract")
        if payload_validator is not None:
            assert collected is not None
            payload_validator(bytes(collected))
        after = os.fstat(fd)
        final = path.stat(follow_symlinks=False)

        def stable(item: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if stable(after) != stable(opened) or stable(final) != stable(opened):
            raise StateError(f"host {kind} artifact changed during verification")
        return VerifiedHostBootArtifact(kind, path, digest, opened.st_size, opened.st_dev, opened.st_ino)
    except FileNotFoundError:
        raise StateError(f"host {kind} artifact is missing") from None
    except OSError:
        raise StateError(f"host {kind} artifact cannot be securely read") from None
    finally:
        if fd is not None:
            os.close(fd)


def verify_host_boot_artifacts(
    kernel: Path,
    initramfs: Path,
    *,
    architecture: str = "x86_64",
    expected_kernel_digest: str | None = None,
    expected_initramfs_digest: str | None = None,
) -> VerifiedHostBootArtifacts:
    """Verify explicitly selected host artifacts; ambient discovery is forbidden."""

    if architecture != "x86_64":
        raise StateError("OCI-root host boot artifacts currently support only x86_64")
    return VerifiedHostBootArtifacts(
        architecture,
        _verify_host_boot_artifact(
            kernel,
            kind="kernel",
            maximum=_MAX_KERNEL_BYTES,
            expected_digest=expected_kernel_digest,
        ),
        _verify_host_boot_artifact(
            initramfs,
            kind="initramfs",
            maximum=_MAX_INITRAMFS_BYTES,
            expected_digest=expected_initramfs_digest,
        ),
    )


def verify_first_party_bootstrap_initramfs(
    path: Path,
    manifest: OCIInitramfsManifest,
) -> VerifiedHostBootArtifact:
    """Pin and structurally verify the bootstrap-only first-party initramfs."""

    if not isinstance(manifest, OCIInitramfsManifest):
        raise StateError("first-party initramfs manifest is invalid")

    def validate(payload: bytes) -> None:
        try:
            verify_bootstrap_initramfs(payload, manifest)
        except ArtifactValidationError:
            raise StateError("first-party initramfs structure or provenance is invalid") from None

    return _verify_host_boot_artifact(
        path,
        kind="initramfs",
        maximum=MAX_OCI_INITRAMFS_BYTES,
        expected_digest=manifest.artifact_digest,
        payload_validator=validate,
    )


@dataclass(frozen=True, slots=True)
class OCIRootDomainPlan:
    run_id: str
    run_name: str
    resource_plan_digest: str
    domain_core_digest: str
    lower_lease_set_id: str
    lower_graph_digest: str
    boot_artifacts: Mapping[str, Any]
    stage1_transport: Mapping[str, Any]
    root_volume: Mapping[str, Any]
    layers: tuple[Mapping[str, Any], ...]
    process: OCIProcessSpec
    memory_mib: int
    vcpus: int
    network: str | None
    kernel_cmdline: str

    def __post_init__(self) -> None:
        try:
            run_id = str(uuid.UUID(self.run_id))
        except (AttributeError, TypeError, ValueError):
            raise StateError("OCI-root domain plan run ID is invalid") from None
        if run_id != self.run_id or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", self.run_name or "") is None:
            raise StateError("OCI-root domain plan run identity is invalid")
        for value, message in (
            (self.resource_plan_digest, "OCI-root resource plan digest is invalid"),
            (self.domain_core_digest, "OCI-root domain core digest is invalid"),
            (self.lower_lease_set_id, "OCI-root lower lease-set ID is invalid"),
            (self.lower_graph_digest, "OCI-root lower graph digest is invalid"),
        ):
            _canonical_digest(value, message)
        boot = dict(self.boot_artifacts) if isinstance(self.boot_artifacts, Mapping) else {}
        if set(boot) != {"architecture", "initramfs", "kernel", "policy"}:
            raise StateError("OCI-root boot artifact contract fields are invalid")
        if boot["architecture"] != "x86_64" or boot["policy"] != OCI_ROOT_BOOT_ARTIFACT_POLICY:
            raise StateError("OCI-root boot artifact policy is invalid")
        for kind in ("kernel", "initramfs"):
            artifact = boot[kind]
            if not isinstance(artifact, Mapping) or set(artifact) != {"digest", "size_bytes"}:
                raise StateError("OCI-root boot artifact identity is invalid")
            _canonical_digest(artifact["digest"], "OCI-root boot artifact digest is invalid")
            if type(artifact["size_bytes"]) is not int or artifact["size_bytes"] <= 0:
                raise StateError("OCI-root boot artifact size is invalid")
        root = dict(self.root_volume) if isinstance(self.root_volume, Mapping) else {}
        if set(root) != {
            "filesystem",
            "filesystem_uuid",
            "generation",
            "serial",
            "size_bytes",
            "target",
            "volume_id",
        }:
            raise StateError("OCI-root domain root volume fields are invalid")
        try:
            volume_id = str(uuid.UUID(root.get("volume_id", "")))
            filesystem_uuid = str(uuid.UUID(root.get("filesystem_uuid", "")))
        except (AttributeError, TypeError, ValueError):
            raise StateError("OCI-root domain root volume ID is invalid") from None
        if (
            volume_id != root["volume_id"]
            or filesystem_uuid != root["filesystem_uuid"]
            or root["filesystem"] != "ext4"
            or root["target"] != "vda"
            or _SERIAL_RE.fullmatch(root["serial"] if isinstance(root["serial"], str) else "") is None
            or root["serial"] != _serial("root", volume_id)
            or type(root["size_bytes"]) is not int
            or root["size_bytes"] <= 0
            or type(root["generation"]) is not int
            or root["generation"] < 1
            or root["generation"] > MAX_OCI_ROOT_VOLUME_GENERATION
        ):
            raise StateError("OCI-root domain root volume identity is invalid")
        if not isinstance(self.layers, tuple) or not self.layers or len(self.layers) > MAX_OCI_ROOT_LAYER_DISKS:
            raise StateError("OCI-root domain lower layers are invalid")
        if not isinstance(self.process, OCIProcessSpec):
            raise StateError("OCI-root domain process contract is invalid")
        try:
            self.process.require_bootable()
        except ArtifactValidationError:
            raise StateError("OCI-root domain process is not bootable") from None
        serials = {root["serial"]}
        for ordinal, raw in enumerate(self.layers):
            layer = dict(raw) if isinstance(raw, Mapping) else {}
            if set(layer) != {
                "filesystem",
                "image_digest",
                "occurrence_digest",
                "ordinal",
                "serial",
                "size_bytes",
                "target",
            }:
                raise StateError("OCI-root domain lower layer fields are invalid")
            serial = layer.get("serial")
            if (
                layer.get("ordinal") != ordinal
                or layer.get("target") != f"vd{chr(ord('c') + ordinal)}"
                or layer.get("filesystem") != "squashfs"
                or _SERIAL_RE.fullmatch(serial if isinstance(serial, str) else "") is None
                or serial != _serial("lower", str(layer.get("occurrence_digest", "")))
                or serial in serials
                or type(layer.get("size_bytes")) is not int
                or layer["size_bytes"] <= 0
            ):
                raise StateError("OCI-root domain lower layer order or identity is invalid")
            _canonical_digest(layer["image_digest"], "OCI-root domain lower image digest is invalid")
            _canonical_digest(layer["occurrence_digest"], "OCI-root domain occurrence digest is invalid")
            serials.add(serial)
        transport = dict(self.stage1_transport) if isinstance(self.stage1_transport, Mapping) else {}
        if set(transport) != _TRANSPORT_RECEIPT_FIELDS | {"serial", "target"}:
            raise StateError("OCI-root stage-1 transport fields are invalid")
        try:
            transport_receipt = OCIStage1TransportReceipt.from_dict(
                {key: transport[key] for key in _TRANSPORT_RECEIPT_FIELDS}
            )
        except ArtifactValidationError:
            raise StateError("OCI-root stage-1 transport receipt is invalid") from None
        transport_serial = transport.get("serial")
        if (
            transport.get("target") != "vdb"
            or _SERIAL_RE.fullmatch(transport_serial if isinstance(transport_serial, str) else "") is None
            or transport_serial != _serial("stage1-transport", transport_receipt.artifact_digest)
            or transport_serial in serials
        ):
            raise StateError("OCI-root stage-1 transport identity is invalid")
        serials.add(transport_serial)
        if not 256 <= self.memory_mib <= 1_048_576 or not 1 <= self.vcpus <= 256:
            raise StateError("OCI-root domain compute shape is invalid")
        if self.network is not None and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", self.network) is None:
            raise StateError("OCI-root domain network is invalid")
        expected_lowers = ",".join(f"virtio-{layer['serial']}" for layer in self.layers)
        expected_cmdline = (
            "console=ttyS0,115200n8 panic=1 rdinit=/init "
            f"palimpsest.resource={self.resource_plan_digest} "
            f"palimpsest.core={self.domain_core_digest} "
            f"palimpsest.stage1={transport_receipt.artifact_digest} "
            f"palimpsest.stage1dev=virtio-{transport_serial} "
            f"palimpsest.root=virtio-{root['serial']} palimpsest.lowers={expected_lowers}"
        )
        if self.kernel_cmdline != expected_cmdline or len(self.kernel_cmdline) > 4096:
            raise StateError("OCI-root domain kernel command line is invalid")
        expected_core = _json_digest(
            _domain_core_dict(
                run_id=self.run_id,
                run_name=self.run_name,
                resource_plan_digest=self.resource_plan_digest,
                lower_lease_set_id=self.lower_lease_set_id,
                lower_graph_digest=self.lower_graph_digest,
                boot_artifacts=boot,
                root_volume=root,
                layers=tuple(self.layers),
                process=self.process,
                memory_mib=self.memory_mib,
                vcpus=self.vcpus,
                network=self.network,
            ),
            "OCI-root domain core is not canonical",
        )
        if expected_core != self.domain_core_digest:
            raise StateError("OCI-root domain core binding is invalid")
        expected_stage1 = OCIStage1Plan.from_domain_resources(
            run_id=self.run_id,
            run_name=self.run_name,
            boot_plan_digest=self.resource_plan_digest,
            domain_core_digest=self.domain_core_digest,
            root_volume=root,
            layers=tuple(self.layers),
            process=self.process,
        )
        expected_transport = build_stage1_transport(expected_stage1)
        if expected_transport.receipt != transport_receipt:
            raise StateError("OCI-root stage-1 transport payload binding is invalid")
        object.__setattr__(self, "boot_artifacts", _frozen_json_object(boot))
        object.__setattr__(self, "stage1_transport", _frozen_json_object(transport))
        object.__setattr__(self, "root_volume", _frozen_json_object(root))
        object.__setattr__(self, "layers", tuple(_frozen_json_object(dict(layer)) for layer in self.layers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "boot_artifacts": _plain_json(self.boot_artifacts),
            "domain_core_digest": self.domain_core_digest,
            "kernel_cmdline": self.kernel_cmdline,
            "layers": _plain_json(self.layers),
            "lower_graph_digest": self.lower_graph_digest,
            "lower_lease_set_id": self.lower_lease_set_id,
            "lifecycle_control": {
                "channel_name": OCI_CONTROL_CHANNEL_NAME,
                "protocol": OCI_CONTROL_PROTOCOL,
                "transport": "virtio-serial",
            },
            "machine": {"memory_mib": self.memory_mib, "network": self.network, "vcpus": self.vcpus},
            "phase": "domain-planned",
            "process": self.process.to_dict(),
            "resource_plan_digest": self.resource_plan_digest,
            "root_volume": _plain_json(self.root_volume),
            "run": {"backend": "kvm", "name": self.run_name, "run_id": self.run_id, "runtime_kind": "oci-root"},
            "schema": OCI_ROOT_DOMAIN_PLAN_SCHEMA,
            "stage1_transport": _plain_json(self.stage1_transport),
        }

    @property
    def digest(self) -> str:
        return _json_digest(self.to_dict(), "OCI-root domain plan is not canonical")

    @classmethod
    def from_dict(cls, value: Any) -> OCIRootDomainPlan:
        if isinstance(value, Mapping) and value.get("schema") in {
            "palimpsest.oci-root-domain-plan.v4",
            "palimpsest.oci-root-domain-plan.v5",
        }:
            version = str(value["schema"]).rsplit(".", 1)[-1]
            raise StateError(f"pre-production OCI-root domain plan {version} is invalidated; rebuild it before launch")
        expected = {
            "boot_artifacts",
            "domain_core_digest",
            "kernel_cmdline",
            "layers",
            "lower_graph_digest",
            "lower_lease_set_id",
            "lifecycle_control",
            "machine",
            "phase",
            "process",
            "resource_plan_digest",
            "root_volume",
            "run",
            "schema",
            "stage1_transport",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise StateError("OCI-root domain plan fields are invalid")
        value = _plain_json(value)
        if value.get("schema") != OCI_ROOT_DOMAIN_PLAN_SCHEMA or value.get("phase") != "domain-planned":
            raise StateError("OCI-root domain plan schema is invalid")
        run = value.get("run")
        machine = value.get("machine")
        lifecycle = value.get("lifecycle_control")
        if (
            not isinstance(run, Mapping)
            or set(run) != {"backend", "name", "run_id", "runtime_kind"}
            or run.get("backend") != "kvm"
            or run.get("runtime_kind") != "oci-root"
            or not isinstance(machine, Mapping)
            or set(machine) != {"memory_mib", "network", "vcpus"}
            or not isinstance(lifecycle, Mapping)
            or lifecycle
            != {
                "channel_name": OCI_CONTROL_CHANNEL_NAME,
                "protocol": OCI_CONTROL_PROTOCOL,
                "transport": "virtio-serial",
            }
            or not isinstance(value.get("layers"), list)
        ):
            raise StateError("OCI-root domain plan dispatch is invalid")
        try:
            plan = cls(
                run_id=run["run_id"],
                run_name=run["name"],
                resource_plan_digest=value["resource_plan_digest"],
                domain_core_digest=value["domain_core_digest"],
                lower_lease_set_id=value["lower_lease_set_id"],
                lower_graph_digest=value["lower_graph_digest"],
                boot_artifacts=value["boot_artifacts"],
                stage1_transport=value["stage1_transport"],
                root_volume=value["root_volume"],
                layers=tuple(value["layers"]),
                process=OCIProcessSpec.from_dict(value["process"]),
                memory_mib=machine["memory_mib"],
                vcpus=machine["vcpus"],
                network=machine["network"],
                kernel_cmdline=value["kernel_cmdline"],
            )
        except (ArtifactValidationError, KeyError, TypeError, ValueError):
            raise StateError("OCI-root domain plan is invalid") from None
        if plan.to_dict() != value:
            raise StateError("OCI-root domain plan is not canonical")
        return plan


@dataclass(frozen=True, slots=True)
class ResolvedOCIRootDomainPlan:
    """Ephemeral path-bearing preview; never a durable or launch authority."""

    plan: OCIRootDomainPlan
    spec: OCIRootDomainSpec
    profile: DomainProfile
    xml: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.plan, OCIRootDomainPlan)
            or not isinstance(self.spec, OCIRootDomainSpec)
            or not isinstance(self.profile, DomainProfile)
            or not isinstance(self.xml, str)
            or not self.xml
        ):
            raise StateError("OCI-root resolved domain preview is invalid")


def _verified_lower_path(roots: StatePaths, digest: str, size: int) -> Path:
    path = roots.store / "blobs" / "sha256" / digest.removeprefix("sha256:")
    try:
        entry = path.stat(follow_symlinks=False)
    except OSError:
        raise StateError("OCI-root lower artifact path is unavailable") from None
    if (
        not path.is_absolute()
        or not stat.S_ISREG(entry.st_mode)
        or entry.st_uid != os.geteuid()
        or entry.st_nlink != 1
        or stat.S_IMODE(entry.st_mode) not in {0o400, 0o444}
        or entry.st_size != size
    ):
        raise StateError("OCI-root lower artifact path is unsafe")
    return path


def build_oci_root_domain_plan(
    roots: StatePaths,
    prepared: PreparedOCIRootRun,
    store: OCIStore,
    boot_artifacts: VerifiedHostBootArtifacts,
    profile: DomainProfile,
    *,
    memory_mib: int = 1024,
    vcpus: int = 1,
    network: str | None = "default",
    runner: CommandRunner = _default_runner,
) -> ResolvedOCIRootDomainPlan:
    """Resolve prepared resources to a non-launching XML preview after validation."""

    if not isinstance(roots, StatePaths) or not isinstance(prepared, PreparedOCIRootRun):
        raise StateError("OCI-root domain planning inputs are invalid")
    if not isinstance(store, OCIStore) or not isinstance(boot_artifacts, VerifiedHostBootArtifacts):
        raise StateError("OCI-root domain planning authorities are invalid")
    transaction = prepared.transaction
    if transaction.phase != "resources-ready" or profile.backend != "kvm" or profile.arch != "x86_64":
        raise StateError("OCI-root domain planning requires ready x86_64 KVM resources")
    revalidated_boot = verify_host_boot_artifacts(
        boot_artifacts.kernel.path,
        boot_artifacts.initramfs.path,
        architecture=boot_artifacts.architecture,
        expected_kernel_digest=boot_artifacts.kernel.digest,
        expected_initramfs_digest=boot_artifacts.initramfs.digest,
    )
    if revalidated_boot != boot_artifacts:
        raise StateError("OCI-root host boot artifact identity changed before domain planning")
    boot_artifacts = revalidated_boot
    leases = store.load_lease_set(
        transaction.lower_lease_set_id,
        transaction.owner,
        plan_digest=transaction.boot_plan_digest,
    )
    if tuple(member.receipt for member in leases.members) != transaction.receipts:
        raise StateError("OCI-root domain lower lease binding is invalid")
    verified_root = load_oci_root_volume(roots, transaction.volume_id, runner=runner)
    root = verified_root.record
    if (
        root.status != "attached"
        or root.attached_run_id != transaction.owner.run_id
        or root.attached_run_name != transaction.owner.run_name
        or root.lower_graph_digest != transaction.lower_graph_digest
        or root.size_bytes != transaction.volume_size_bytes
        or root.retention_policy != transaction.retention_policy
    ):
        raise StateError("OCI-root domain root volume binding is invalid")
    root_serial = _serial("root", root.volume_id)
    root_contract = {
        "filesystem": "ext4",
        "filesystem_uuid": verified_root.filesystem_uuid,
        "generation": root.generation,
        "serial": root_serial,
        "size_bytes": root.size_bytes,
        "target": "vda",
        "volume_id": root.volume_id,
    }
    layers: list[dict[str, Any]] = []
    layer_disks: list[LayerDisk] = []
    serials = {root_serial}
    for member in leases.members:
        receipt = member.receipt
        serial = _serial("lower", receipt.occurrence_digest)
        if serial in serials:
            raise StateError("OCI-root domain disk serial collision")
        serials.add(serial)
        target = f"vd{chr(ord('c') + member.ordinal)}"
        path = _verified_lower_path(roots, receipt.image_digest, receipt.image_size)
        layers.append(
            {
                "image_digest": receipt.image_digest,
                "filesystem": receipt.filesystem,
                "occurrence_digest": receipt.occurrence_digest,
                "ordinal": member.ordinal,
                "serial": serial,
                "size_bytes": receipt.image_size,
                "target": target,
            }
        )
        layer_disks.append(LayerDisk(receipt.image_digest, path, target, serial))
    process = OCIProcessSpec.from_dict(transaction.boot_plan["process"])
    core = _domain_core_dict(
        run_id=transaction.owner.run_id,
        run_name=transaction.owner.run_name,
        resource_plan_digest=transaction.boot_plan_digest,
        lower_lease_set_id=transaction.lower_lease_set_id,
        lower_graph_digest=transaction.lower_graph_digest,
        boot_artifacts=boot_artifacts.to_dict(),
        root_volume=root_contract,
        layers=tuple(layers),
        process=process,
        memory_mib=memory_mib,
        vcpus=vcpus,
        network=network,
    )
    domain_core_digest = _json_digest(core, "OCI-root domain core is not canonical")
    stage1_plan = OCIStage1Plan.from_domain_resources(
        run_id=transaction.owner.run_id,
        run_name=transaction.owner.run_name,
        boot_plan_digest=transaction.boot_plan_digest,
        domain_core_digest=domain_core_digest,
        root_volume=root_contract,
        layers=tuple(layers),
        process=process,
    )
    transport = build_stage1_transport(stage1_plan)
    transport_serial = _serial("stage1-transport", transport.receipt.artifact_digest)
    transport_contract = {
        **transport.receipt.to_dict(),
        "serial": transport_serial,
        "target": "vdb",
    }
    lower_ids = ",".join(f"virtio-{layer['serial']}" for layer in layers)
    cmdline = (
        "console=ttyS0,115200n8 panic=1 rdinit=/init "
        f"palimpsest.resource={transaction.boot_plan_digest} "
        f"palimpsest.core={domain_core_digest} "
        f"palimpsest.stage1={transport.receipt.artifact_digest} "
        f"palimpsest.stage1dev=virtio-{transport_serial} "
        f"palimpsest.root=virtio-{root_serial} palimpsest.lowers={lower_ids}"
    )
    plan = OCIRootDomainPlan(
        run_id=transaction.owner.run_id,
        run_name=transaction.owner.run_name,
        resource_plan_digest=transaction.boot_plan_digest,
        domain_core_digest=domain_core_digest,
        lower_lease_set_id=transaction.lower_lease_set_id,
        lower_graph_digest=transaction.lower_graph_digest,
        boot_artifacts=boot_artifacts.to_dict(),
        stage1_transport=transport_contract,
        root_volume=root_contract,
        layers=tuple(layers),
        process=process,
        memory_mib=memory_mib,
        vcpus=vcpus,
        network=network,
        kernel_cmdline=cmdline,
    )
    transport_path = run_paths(roots, plan.run_name).root / OCI_STAGE1_TRANSPORT_FILENAME
    spec = OCIRootDomainSpec(
        name=plan.run_name,
        memory_mib=memory_mib,
        vcpus=vcpus,
        kernel=boot_artifacts.kernel.path,
        initramfs=boot_artifacts.initramfs.path,
        kernel_cmdline=cmdline,
        root_disk=verified_root.path,
        root_serial=root_serial,
        layers=tuple(layer_disks),
        stage1_transport=Stage1TransportDisk(
            transport.receipt.artifact_digest,
            transport_path,
            "vdb",
            transport_serial,
        ),
        network=network,
        run_id=plan.run_id,
        boot_contract_digest=plan.digest,
    )
    try:
        xml = build_oci_root_domain_xml(spec, profile)
    except KvmError as exc:
        raise StateError("OCI-root KVM domain contract is invalid") from exc
    return ResolvedOCIRootDomainPlan(plan, spec, profile, xml)


def commit_oci_root_domain_plan(
    roots: StatePaths,
    resolved: ResolvedOCIRootDomainPlan,
    store: OCIStore,
    *,
    runner: CommandRunner = _default_runner,
) -> OCIRootDomainPlan:
    """Commit only the path-free handoff to the existing preparation ledger."""

    if not isinstance(resolved, ResolvedOCIRootDomainPlan):
        raise StateError("OCI-root resolved domain plan is invalid")
    if not isinstance(store, OCIStore):
        raise StateError("OCI-root domain commit store is invalid")
    plan = OCIRootDomainPlan.from_dict(resolved.plan.to_dict())
    if plan != resolved.plan:
        raise StateError("OCI-root resolved domain plan changed")
    snapshot = read_run_ledger_snapshot(roots, plan.run_name)
    if (
        snapshot.record.run_id != plan.run_id
        or snapshot.record.dispatch_key.runtime_kind is not RuntimeKind.OCI_ROOT
        or snapshot.record.dispatch_key.backend is not RuntimeBackend.KVM
    ):
        raise StateError("OCI-root domain plan does not match the run ledger")
    transaction = OCIRootPreparationTransaction.from_dict(snapshot.state.get("oci_root"))
    if transaction.phase != "resources-ready":
        raise StateError("OCI-root resources are not ready for domain planning")
    if (
        transaction.boot_plan_digest != plan.resource_plan_digest
        or transaction.lower_lease_set_id != plan.lower_lease_set_id
        or transaction.lower_graph_digest != plan.lower_graph_digest
        or transaction.volume_id != plan.root_volume["volume_id"]
        or transaction.volume_size_bytes != plan.root_volume["size_bytes"]
        or transaction.owner.run_id != plan.run_id
        or transaction.owner.run_name != plan.run_name
    ):
        raise StateError("OCI-root domain plan resource binding is invalid")
    leases = store.load_lease_set(
        transaction.lower_lease_set_id,
        transaction.owner,
        plan_digest=transaction.boot_plan_digest,
    )
    expected_layers: list[dict[str, Any]] = []
    expected_disks: list[LayerDisk] = []
    for member in leases.members:
        receipt = member.receipt
        serial = _serial("lower", receipt.occurrence_digest)
        target = f"vd{chr(ord('c') + member.ordinal)}"
        path = _verified_lower_path(roots, receipt.image_digest, receipt.image_size)
        expected_layers.append(
            {
                "image_digest": receipt.image_digest,
                "filesystem": receipt.filesystem,
                "occurrence_digest": receipt.occurrence_digest,
                "ordinal": member.ordinal,
                "serial": serial,
                "size_bytes": receipt.image_size,
                "target": target,
            }
        )
        expected_disks.append(LayerDisk(receipt.image_digest, path, target, serial))
    if _plain_json(plan.layers) != expected_layers:
        raise StateError("OCI-root domain plan lower lease binding is invalid")
    if plan.process != OCIProcessSpec.from_dict(transaction.boot_plan["process"]):
        raise StateError("OCI-root domain plan process binding is invalid")
    verified_root = load_oci_root_volume(roots, transaction.volume_id, runner=runner)
    root = verified_root.record
    expected_root = {
        "filesystem": "ext4",
        "filesystem_uuid": verified_root.filesystem_uuid,
        "generation": root.generation,
        "serial": _serial("root", root.volume_id),
        "size_bytes": root.size_bytes,
        "target": "vda",
        "volume_id": root.volume_id,
    }
    if (
        _plain_json(plan.root_volume) != expected_root
        or root.status != "attached"
        or root.attached_run_id != transaction.owner.run_id
        or root.attached_run_name != transaction.owner.run_name
        or root.lower_graph_digest != transaction.lower_graph_digest
        or root.retention_policy != transaction.retention_policy
    ):
        raise StateError("OCI-root domain plan root volume binding is invalid")
    boot = verify_host_boot_artifacts(
        resolved.spec.kernel,
        resolved.spec.initramfs,
        architecture="x86_64",
        expected_kernel_digest=str(plan.boot_artifacts["kernel"]["digest"]),
        expected_initramfs_digest=str(plan.boot_artifacts["initramfs"]["digest"]),
    )
    if boot.to_dict() != _plain_json(plan.boot_artifacts):
        raise StateError("OCI-root domain plan boot artifact binding is invalid")
    try:
        transport_receipt = OCIStage1TransportReceipt.from_dict(
            {key: plan.stage1_transport[key] for key in _TRANSPORT_RECEIPT_FIELDS}
        )
    except (ArtifactValidationError, KeyError, TypeError):
        raise StateError("OCI-root stage-1 transport receipt binding is invalid") from None
    expected_stage1 = OCIStage1Plan.from_domain_plan(plan)
    built_transport = build_stage1_transport(expected_stage1)
    expected_transport_contract = {
        **built_transport.receipt.to_dict(),
        "serial": _serial("stage1-transport", built_transport.receipt.artifact_digest),
        "target": "vdb",
    }
    if (
        transport_receipt != built_transport.receipt
        or _plain_json(plan.stage1_transport) != expected_transport_contract
    ):
        raise StateError("OCI-root stage-1 transport projection is invalid")
    transport_path = run_paths(roots, plan.run_name).root / OCI_STAGE1_TRANSPORT_FILENAME
    expected_spec = OCIRootDomainSpec(
        name=plan.run_name,
        memory_mib=plan.memory_mib,
        vcpus=plan.vcpus,
        kernel=boot.kernel.path,
        initramfs=boot.initramfs.path,
        kernel_cmdline=plan.kernel_cmdline,
        root_disk=verified_root.path,
        root_serial=str(plan.root_volume["serial"]),
        layers=tuple(expected_disks),
        stage1_transport=Stage1TransportDisk(
            built_transport.receipt.artifact_digest,
            transport_path,
            "vdb",
            str(plan.stage1_transport["serial"]),
        ),
        network=plan.network,
        run_id=plan.run_id,
        boot_contract_digest=plan.digest,
    )
    try:
        expected_xml = build_oci_root_domain_xml(expected_spec, resolved.profile)
    except KvmError as exc:
        raise StateError("OCI-root resolved domain profile is invalid") from exc
    if resolved.spec != expected_spec or resolved.xml != expected_xml:
        raise StateError("OCI-root resolved domain XML binding is invalid")
    with locked_existing_run(roots, plan.run_name, expected_snapshot=snapshot) as mutation:
        data = mutation.mutable_state()
        if "oci_root_domain" in data:
            raise StateError("OCI-root domain plan is already committed")
        mutation.write_file(OCI_STAGE1_TRANSPORT_FILENAME, built_transport.artifact, mode=0o400)
        verified_transport = verify_stage1_transport_file(
            transport_path,
            built_transport.receipt,
            expected_stage1_plan=expected_stage1,
        )
        if verified_transport.plan != expected_stage1:
            raise StateError("OCI-root stage-1 transport changed during commit")
        data.pop("status", None)
        data.pop("lifecycle_revision", None)
        data["oci_root_domain"] = {"digest": plan.digest, "plan": plan.to_dict()}
        mutation.write_state("creating", data)
    return plan


def load_oci_root_domain_plan(roots: StatePaths, name: str) -> OCIRootDomainPlan:
    snapshot = read_run_ledger_snapshot(roots, name)
    value = snapshot.state.get("oci_root_domain")
    if not isinstance(value, Mapping) or set(value) != {"digest", "plan"}:
        raise StateError("OCI-root domain plan ledger is missing or invalid")
    plan = OCIRootDomainPlan.from_dict(value.get("plan"))
    digest = _canonical_digest(value.get("digest"), "OCI-root domain plan ledger digest is invalid")
    if digest != plan.digest or plan.run_id != snapshot.record.run_id or plan.run_name != snapshot.record.name:
        raise StateError("OCI-root domain plan ledger binding is invalid")
    try:
        transport_receipt = OCIStage1TransportReceipt.from_dict(
            {key: plan.stage1_transport[key] for key in _TRANSPORT_RECEIPT_FIELDS}
        )
    except (ArtifactValidationError, KeyError, TypeError):
        raise StateError("OCI-root stage-1 transport ledger binding is invalid") from None
    expected_stage1 = OCIStage1Plan.from_domain_plan(plan)
    verified_transport = verify_stage1_transport_file(
        run_paths(roots, name).root / OCI_STAGE1_TRANSPORT_FILENAME,
        transport_receipt,
        expected_stage1_plan=expected_stage1,
    )
    if verified_transport.plan != expected_stage1:
        raise StateError("OCI-root stage-1 transport ledger projection is invalid")
    return plan


__all__ = [
    "OCI_ROOT_BOOT_ARTIFACT_POLICY",
    "OCI_ROOT_DOMAIN_PLAN_SCHEMA",
    "OCIRootDomainPlan",
    "ResolvedOCIRootDomainPlan",
    "VerifiedHostBootArtifact",
    "VerifiedHostBootArtifacts",
    "build_oci_root_domain_plan",
    "commit_oci_root_domain_plan",
    "load_oci_root_domain_plan",
    "verify_host_boot_artifacts",
    "verify_first_party_bootstrap_initramfs",
]
