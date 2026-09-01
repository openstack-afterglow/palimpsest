"""Path-free guest stage-1 assembly and workload handoff contract.

This module defines the contract consumed by the packaged first-party
``/init``.  It does not perform OCI filesystem mounts, pivot root, supervise
the image process as PID 1, or enable the OCI-root runtime.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .digest import normalize_digest
from .errors import ArtifactValidationError, InvalidDigestError, StateError
from .kvm import MAX_OCI_ROOT_LAYER_DISKS
from .oci_packer import SQUASHFS_BLOCK_DEVICE_ALIGNMENT
from .oci_process import OCIProcessSpec
from .oci_provenance import canonical_json_bytes
from .oci_root_volume import MAX_OCI_ROOT_VOLUME_GENERATION
from .oci_store import MAX_OCI_STORE_IMAGE_BYTES
from .project_volumes import MAX_VOLUME_BYTES, MIN_VOLUME_BYTES

if TYPE_CHECKING:
    from .oci_root_kvm import OCIRootDomainPlan

OCI_STAGE1_PLAN_SCHEMA = "palimpsest.oci-stage1-plan.v3"
OCI_STAGE1_PROTOCOL = "palimpsest.guest-stage1.v3"
OCI_STAGE1_DEVICE_POLICY = "virtio-serial-sysfs.v1"
_RUN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SERIAL_RE = re.compile(r"^[0-9a-f]{20}$")


def _canonical_digest(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return normalize_digest(value) == value
    except (ArtifactValidationError, InvalidDigestError, TypeError, ValueError):
        return False


def oci_stage1_device_serial(namespace: str, identity: str) -> str:
    """Derive the shared domain/stage-1 virtio serial."""

    if namespace not in {"root", "lower", "stage1-transport"} or not isinstance(identity, str) or not identity:
        raise StateError("stage-1 device serial input is invalid")
    return hashlib.sha256(f"palimpsest-oci-root-{namespace}-v1\0{identity}".encode()).hexdigest()[:20]


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _freeze(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            key: _freeze(item)
            if isinstance(item, Mapping)
            else tuple(_freeze(child) if isinstance(child, Mapping) else child for child in item)
            if isinstance(item, (list, tuple))
            else item
            for key, item in value.items()
        }
    )


@dataclass(frozen=True, slots=True)
class OCIStage1Plan:
    run_id: str
    run_name: str
    boot_plan_digest: str
    domain_core_digest: str
    root: Mapping[str, Any]
    layers: tuple[Mapping[str, Any], ...]
    process: OCIProcessSpec

    def __post_init__(self) -> None:
        try:
            parsed = str(uuid.UUID(self.run_id))
        except (AttributeError, TypeError, ValueError):
            raise StateError("stage-1 run ID is invalid") from None
        if parsed != self.run_id or _RUN_NAME_RE.fullmatch(self.run_name or "") is None:
            raise StateError("stage-1 run identity is invalid")
        for value in (self.boot_plan_digest, self.domain_core_digest):
            if not _canonical_digest(value):
                raise StateError("stage-1 plan digest is invalid") from None
        root = _plain(self.root)
        if not isinstance(root, dict) or set(root) != {
            "filesystem",
            "generation",
            "mount_options",
            "serial",
            "size_bytes",
            "volume_id",
        }:
            raise StateError("stage-1 root contract fields are invalid")
        try:
            volume_id = str(uuid.UUID(root["volume_id"]))
        except (AttributeError, TypeError, ValueError):
            raise StateError("stage-1 root identity is invalid") from None
        if (
            root["filesystem"] != "ext4"
            or root["mount_options"] != ["rw", "nodev", "nosuid"]
            or volume_id != root["volume_id"]
            or type(root["generation"]) is not int
            or root["generation"] < 1
            or root["generation"] > MAX_OCI_ROOT_VOLUME_GENERATION
            or type(root["size_bytes"]) is not int
            or not MIN_VOLUME_BYTES <= root["size_bytes"] <= MAX_VOLUME_BYTES
            or root["size_bytes"] % (1024 * 1024) != 0
            or _SERIAL_RE.fullmatch(root["serial"] if isinstance(root["serial"], str) else "") is None
            or root["serial"] != oci_stage1_device_serial("root", volume_id)
        ):
            raise StateError("stage-1 root mount policy is invalid")
        if not isinstance(self.layers, tuple) or not 1 <= len(self.layers) <= MAX_OCI_ROOT_LAYER_DISKS:
            raise StateError("stage-1 lower contract is invalid")
        layers = tuple(_plain(layer) for layer in self.layers)
        serials = {root["serial"]}
        for ordinal, layer in enumerate(layers):
            if not isinstance(layer, dict) or set(layer) != {
                "filesystem",
                "image_digest",
                "mount_options",
                "occurrence_digest",
                "ordinal",
                "serial",
                "size_bytes",
            }:
                raise StateError("stage-1 lower fields are invalid")
            if (
                layer["ordinal"] != ordinal
                or layer["filesystem"] != "squashfs"
                or layer["mount_options"] != ["ro", "nodev", "nosuid"]
                or _SERIAL_RE.fullmatch(layer["serial"] if isinstance(layer["serial"], str) else "") is None
                or layer["serial"] in serials
                or not _canonical_digest(layer["image_digest"])
                or not _canonical_digest(layer["occurrence_digest"])
                or type(layer["size_bytes"]) is not int
                or not SQUASHFS_BLOCK_DEVICE_ALIGNMENT <= layer["size_bytes"] <= MAX_OCI_STORE_IMAGE_BYTES
                or layer["size_bytes"] % SQUASHFS_BLOCK_DEVICE_ALIGNMENT != 0
                or layer["serial"] != oci_stage1_device_serial("lower", layer["occurrence_digest"])
            ):
                raise StateError("stage-1 lower mount policy is invalid")
            serials.add(layer["serial"])
        if not isinstance(self.process, OCIProcessSpec):
            raise StateError("stage-1 process contract is invalid")
        try:
            self.process.require_bootable()
        except ArtifactValidationError:
            raise StateError("stage-1 process is not bootable") from None
        object.__setattr__(self, "root", _freeze(root))
        object.__setattr__(self, "layers", tuple(_freeze(layer) for layer in layers))

    @classmethod
    def from_domain_resources(
        cls,
        *,
        run_id: str,
        run_name: str,
        boot_plan_digest: str,
        domain_core_digest: str,
        root_volume: Mapping[str, Any],
        layers: tuple[Mapping[str, Any], ...],
        process: OCIProcessSpec,
    ) -> OCIStage1Plan:
        """Build the guest projection without depending on the final domain digest."""

        try:
            return cls(
                run_id=run_id,
                run_name=run_name,
                boot_plan_digest=boot_plan_digest,
                domain_core_digest=domain_core_digest,
                root={
                    "filesystem": root_volume["filesystem"],
                    "generation": root_volume["generation"],
                    "mount_options": ["rw", "nodev", "nosuid"],
                    "serial": root_volume["serial"],
                    "size_bytes": root_volume["size_bytes"],
                    "volume_id": root_volume["volume_id"],
                },
                layers=tuple(
                    {
                        "filesystem": layer["filesystem"],
                        "image_digest": layer["image_digest"],
                        "mount_options": ["ro", "nodev", "nosuid"],
                        "occurrence_digest": layer["occurrence_digest"],
                        "ordinal": layer["ordinal"],
                        "serial": layer["serial"],
                        "size_bytes": layer["size_bytes"],
                    }
                    for layer in layers
                ),
                process=process,
            )
        except (KeyError, TypeError):
            raise StateError("stage-1 domain resources are invalid") from None

    @classmethod
    def from_domain_plan(cls, plan: OCIRootDomainPlan) -> OCIStage1Plan:
        from .oci_root_kvm import OCIRootDomainPlan

        if not isinstance(plan, OCIRootDomainPlan):
            raise StateError("stage-1 requires an OCI-root domain plan")
        return cls.from_domain_resources(
            run_id=plan.run_id,
            run_name=plan.run_name,
            boot_plan_digest=plan.resource_plan_digest,
            domain_core_digest=plan.domain_core_digest,
            root_volume=plan.root_volume,
            layers=plan.layers,
            process=plan.process,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assembly": {
                "device_policy": OCI_STAGE1_DEVICE_POLICY,
                "layers": _plain(self.layers),
                "lowerdir_ordinals": list(reversed(range(len(self.layers)))),
                "overlay_mount_options": ["rw", "nodev", "nosuid"],
                "root": _plain(self.root),
            },
            "boot_plan_digest": self.boot_plan_digest,
            "domain_core_digest": self.domain_core_digest,
            "handoff": "first-party-pid1-supervisor-required",
            "phase": "stage1-contract",
            "process": self.process.to_dict(),
            "protocol": OCI_STAGE1_PROTOCOL,
            "run": {"name": self.run_name, "run_id": self.run_id},
            "schema": OCI_STAGE1_PLAN_SCHEMA,
        }

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()}"

    @classmethod
    def from_dict(cls, value: Any, *, expected_domain_plan: OCIRootDomainPlan) -> OCIStage1Plan:
        from .oci_root_kvm import OCIRootDomainPlan

        if not isinstance(expected_domain_plan, OCIRootDomainPlan):
            raise StateError("stage-1 wire requires an expected OCI-root domain plan")
        if not isinstance(value, Mapping) or set(value) != {
            "assembly",
            "boot_plan_digest",
            "domain_core_digest",
            "handoff",
            "phase",
            "process",
            "protocol",
            "run",
            "schema",
        }:
            raise StateError("stage-1 plan fields are invalid")
        value = _plain(value)
        assembly = value.get("assembly")
        run = value.get("run")
        if (
            value.get("schema") != OCI_STAGE1_PLAN_SCHEMA
            or value.get("protocol") != OCI_STAGE1_PROTOCOL
            or value.get("phase") != "stage1-contract"
            or value.get("handoff") != "first-party-pid1-supervisor-required"
            or not isinstance(run, dict)
            or set(run) != {"name", "run_id"}
            or not isinstance(assembly, dict)
            or set(assembly) != {"device_policy", "layers", "lowerdir_ordinals", "overlay_mount_options", "root"}
            or assembly.get("device_policy") != OCI_STAGE1_DEVICE_POLICY
            or assembly.get("overlay_mount_options") != ["rw", "nodev", "nosuid"]
            or not isinstance(assembly.get("layers"), list)
            or assembly.get("lowerdir_ordinals") != list(reversed(range(len(assembly["layers"]))))
        ):
            raise StateError("stage-1 plan policy is invalid")
        try:
            process = OCIProcessSpec.from_dict(value["process"])
        except ArtifactValidationError:
            raise StateError("stage-1 process contract is invalid") from None
        plan = cls(
            run_id=run["run_id"],
            run_name=run["name"],
            boot_plan_digest=value["boot_plan_digest"],
            domain_core_digest=value["domain_core_digest"],
            root=assembly["root"],
            layers=tuple(assembly["layers"]),
            process=process,
        )
        if plan.to_dict() != value:
            raise StateError("stage-1 plan is not canonical")
        if plan.to_dict() != cls.from_domain_plan(expected_domain_plan).to_dict():
            raise StateError("stage-1 plan does not match the expected domain plan")
        return plan


__all__ = [
    "OCI_STAGE1_DEVICE_POLICY",
    "OCI_STAGE1_PLAN_SCHEMA",
    "OCI_STAGE1_PROTOCOL",
    "OCIStage1Plan",
    "oci_stage1_device_serial",
]
