"""Path-free OCI-root boot intent and durable immutable-lower reservation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .oci_materializer import OCIImageMaterializationReceipt
from .oci_provenance import canonical_json_bytes
from .oci_store import (
    ArtifactLeaseOwner,
    DerivedLayerOccurrence,
    DerivedLayerReceipt,
    DurableLeaseSet,
    OCIStore,
    OCIStoreError,
)

OCI_ROOT_BOOT_PLAN_SCHEMA = "palimpsest.oci-root-boot-plan.v1"
OCI_ROOT_LOWER_ROLE = "root-lower"


@dataclass(frozen=True, slots=True)
class OCIBootPlanIntent:
    """Canonical intent to reserve one materialized OCI graph as VM lowers."""

    run_id: str
    run_name: str
    materialization: OCIImageMaterializationReceipt

    def __post_init__(self) -> None:
        ArtifactLeaseOwner(self.run_id, self.run_name, OCI_ROOT_LOWER_ROLE)
        if not isinstance(self.materialization, OCIImageMaterializationReceipt):
            raise OCIStoreError("oci-boot-plan", "boot-plan materialization is invalid")

    @property
    def owner(self) -> ArtifactLeaseOwner:
        return ArtifactLeaseOwner(self.run_id, self.run_name, OCI_ROOT_LOWER_ROLE)

    @property
    def receipts(self) -> tuple[DerivedLayerReceipt, ...]:
        return tuple(result.receipt for result in self.materialization.results)

    @property
    def occurrences(self) -> tuple[DerivedLayerOccurrence, ...]:
        materialization = self.materialization
        return tuple(
            DerivedLayerOccurrence(
                source_snapshot_binding_digest=materialization.source_snapshot_binding_digest,
                source_image_digest=materialization.source_image_digest,
                ordinal=ordinal,
                media_type=descriptor.media_type,
                compressed_digest=descriptor.digest,
                compressed_size=descriptor.size,
                diff_id=diff_id,
            )
            for ordinal, (descriptor, diff_id) in enumerate(
                zip(materialization.layer_descriptors, materialization.layer_diff_ids, strict=True)
            )
        )

    def lower_graph_dict(self) -> dict[str, Any]:
        """Return the immutable OCI graph identity, independent of one run."""
        materialization = self.materialization
        return {
            "config_descriptor": materialization.config_descriptor.to_dict(),
            "layers": [
                {
                    "compressed": descriptor.to_dict(),
                    "derived_receipt": result.receipt.to_dict(),
                    "diff_id": diff_id,
                    "ordinal": ordinal,
                }
                for ordinal, (descriptor, diff_id, result) in enumerate(
                    zip(
                        materialization.layer_descriptors,
                        materialization.layer_diff_ids,
                        materialization.results,
                        strict=True,
                    )
                )
            ],
            "manifest_digest": materialization.manifest_digest,
            "platform": {
                "architecture": materialization.platform_architecture,
                "os": materialization.platform_os,
            },
            "root_descriptor": materialization.root_descriptor.to_dict(),
            "source_image_digest": materialization.source_image_digest,
            "source_snapshot_binding_digest": materialization.source_snapshot_binding_digest,
        }

    @property
    def lower_graph_digest(self) -> str:
        return f"sha256:{hashlib.sha256(canonical_json_bytes(self.lower_graph_dict())).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        lower_graph = self.lower_graph_dict()
        return {
            **lower_graph,
            "lower_graph_digest": self.lower_graph_digest,
            "phase": "lower-reserved",
            "retention": "durable-lease-set",
            "run": {
                "backend": "kvm",
                "name": self.owner.run_name,
                "run_id": self.owner.run_id,
                "runtime_kind": "oci-root",
            },
            "schema": OCI_ROOT_BOOT_PLAN_SCHEMA,
            "writable_root_policy": "vm-specific",
        }

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()}"


@dataclass(frozen=True, slots=True)
class PreparedOCIBootPlan:
    """A canonical intent whose complete ordered lower set is durably retained."""

    intent: OCIBootPlanIntent
    lower_leases: DurableLeaseSet

    def __post_init__(self) -> None:
        if not isinstance(self.intent, OCIBootPlanIntent) or not isinstance(self.lower_leases, DurableLeaseSet):
            raise OCIStoreError("oci-boot-plan", "prepared boot-plan inputs are invalid")
        if (
            self.lower_leases.plan_digest != self.intent.digest
            or self.lower_leases.owner != self.intent.owner
            or tuple(member.receipt for member in self.lower_leases.members) != self.intent.receipts
        ):
            raise OCIStoreError("oci-boot-plan", "prepared boot-plan lease binding is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "intent_digest": self.intent.digest,
            "lease_set_id": self.lower_leases.lease_set_id,
            "lower_leases": [
                {
                    "acquired_ns": member.acquired_ns,
                    "lease_id": member.lease_id,
                    "ordinal": member.ordinal,
                    "receipt": member.receipt.to_dict(),
                }
                for member in self.lower_leases.members
            ],
            "schema": "palimpsest.oci-root-prepared-boot-plan.v1",
        }


def prepare_oci_boot_plan(
    materialization: OCIImageMaterializationReceipt,
    *,
    run_id: str,
    run_name: str,
    store: OCIStore,
) -> PreparedOCIBootPlan:
    """Create or crash-recover the deterministic lower reservation for a run."""
    if not isinstance(store, OCIStore):
        raise OCIStoreError("oci-boot-plan", "boot-plan store is invalid")
    intent = OCIBootPlanIntent(run_id, run_name, materialization)
    store.validate_receipt_occurrences(intent.receipts, intent.occurrences)
    lower_leases = store.acquire_lease_set(
        intent.receipts,
        intent.owner,
        plan_digest=intent.digest,
    )
    return PreparedOCIBootPlan(intent, lower_leases)


def load_prepared_oci_boot_plan(intent: OCIBootPlanIntent, store: OCIStore) -> PreparedOCIBootPlan:
    """Recover a known intent without creating a different reservation."""
    if not isinstance(intent, OCIBootPlanIntent) or not isinstance(store, OCIStore):
        raise OCIStoreError("oci-boot-plan", "boot-plan recovery input is invalid")
    store.validate_receipt_occurrences(intent.receipts, intent.occurrences)
    expected = store.lease_set_id(intent.receipts, intent.owner, plan_digest=intent.digest)
    lower_leases = store.load_lease_set(expected, intent.owner, plan_digest=intent.digest)
    return PreparedOCIBootPlan(intent, lower_leases)


def release_oci_boot_plan(prepared: PreparedOCIBootPlan, store: OCIStore) -> None:
    """Release a prepared lower reservation after run teardown or rollback."""
    if not isinstance(prepared, PreparedOCIBootPlan) or not isinstance(store, OCIStore):
        raise OCIStoreError("oci-boot-plan", "boot-plan release input is invalid")
    store.release_lease_set(prepared.lower_leases)


__all__ = [
    "OCI_ROOT_BOOT_PLAN_SCHEMA",
    "OCI_ROOT_LOWER_ROLE",
    "OCIBootPlanIntent",
    "PreparedOCIBootPlan",
    "load_prepared_oci_boot_plan",
    "prepare_oci_boot_plan",
    "release_oci_boot_plan",
]
