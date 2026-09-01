"""Durable OCI-root lower and writable-volume preparation transaction."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .digest import normalize_digest
from .errors import ArtifactValidationError, StateError
from .oci_boot_plan import OCIBootPlanIntent, PreparedOCIBootPlan, prepare_oci_boot_plan
from .oci_materializer import OCIImageMaterializationReceipt
from .oci_process import OCIProcessSpec
from .oci_provenance import canonical_json_bytes
from .oci_root_volume import (
    ClaimedOCIRootVolume,
    OCIRootVolumeRecord,
    claim_oci_root_volume,
    load_oci_root_volume,
    new_oci_root_volume_id,
    release_oci_root_volume,
    rollback_oci_root_volume_claim,
)
from .oci_store import (
    ArtifactLeaseOwner,
    DerivedLayerOccurrence,
    DerivedLayerReceipt,
    DurableLeaseSet,
    OCIStore,
    OCIStoreError,
)
from .project_volumes import CommandRunner, _default_runner, _validate_size
from .runtime_types import RuntimeBackend, RuntimeKind
from .state import (
    NewRunReservation,
    RunLedgerSnapshot,
    StatePaths,
    locked_existing_run,
    read_run_ledger_snapshot,
    utc_now_iso,
)

OCI_ROOT_PREPARATION_SCHEMA = "palimpsest.oci-root-run-prepare.v2"
_PHASES = frozenset(
    {
        "resources-planned",
        "resources-ready",
        "rollback-required",
        "rolled-back",
        "release-required",
        "released",
    }
)
_ROLLBACK_ACTIONS = frozenset({"delete", "retain"})
_BOOT_PLAN_FIELDS = frozenset(
    {
        "config_descriptor",
        "layers",
        "lower_graph_digest",
        "manifest_digest",
        "phase",
        "platform",
        "process",
        "retention",
        "root_descriptor",
        "run",
        "schema",
        "source_image_digest",
        "source_snapshot_binding_digest",
        "writable_root_policy",
    }
)
_LOWER_GRAPH_FIELDS = frozenset(
    {
        "config_descriptor",
        "layers",
        "manifest_digest",
        "platform",
        "root_descriptor",
        "source_image_digest",
        "source_snapshot_binding_digest",
    }
)


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _json_digest(value: Any, message: str) -> str:
    try:
        return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"
    except ArtifactValidationError:
        raise StateError(message) from None


def _canonical_digest(value: str, message: str) -> str:
    try:
        normalized = normalize_digest(value)
    except (ArtifactValidationError, TypeError, ValueError):
        raise StateError(message) from None
    if normalized != value:
        raise StateError(message)
    return normalized


@dataclass(frozen=True, slots=True)
class OCIRootPreparationTransaction:
    phase: str
    boot_plan: Mapping[str, Any]
    boot_plan_digest: str
    lower_lease_set_id: str
    volume_id: str
    volume_size_bytes: int
    lower_graph_digest: str
    retention_policy: str
    rollback_action: str

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise StateError("OCI-root preparation phase is invalid")
        if not isinstance(self.boot_plan, Mapping):
            raise StateError("OCI-root preparation boot plan is invalid")
        plan = _plain_json(self.boot_plan)
        if not isinstance(plan, dict):
            raise StateError("OCI-root preparation boot plan is invalid")
        plan_digest = _canonical_digest(self.boot_plan_digest, "OCI-root preparation plan digest is invalid")
        if set(plan) != _BOOT_PLAN_FIELDS:
            raise StateError("OCI-root preparation boot plan fields are invalid")
        actual = _json_digest(plan, "OCI-root preparation boot plan is invalid")
        if actual != plan_digest:
            raise StateError("OCI-root preparation boot plan binding is invalid")
        lower_set = _canonical_digest(self.lower_lease_set_id, "OCI-root preparation lease-set ID is invalid")
        lower_graph = _canonical_digest(self.lower_graph_digest, "OCI-root preparation lower graph digest is invalid")
        graph = {key: plan[key] for key in _LOWER_GRAPH_FIELDS if key in plan}
        if set(graph) != _LOWER_GRAPH_FIELDS:
            raise StateError("OCI-root preparation lower graph is incomplete")
        actual_graph = _json_digest(graph, "OCI-root preparation lower graph is invalid")
        if actual_graph != lower_graph or plan.get("lower_graph_digest") != lower_graph:
            raise StateError("OCI-root preparation lower graph binding is invalid")
        if plan.get("schema") != "palimpsest.oci-root-boot-plan.v2":
            raise StateError("OCI-root preparation boot plan schema is invalid")
        try:
            OCIProcessSpec.from_dict(plan.get("process")).require_bootable()
        except (ArtifactValidationError, TypeError, ValueError):
            raise StateError("OCI-root preparation process contract is invalid") from None
        if (
            plan.get("phase") != "lower-reserved"
            or plan.get("retention") != "durable-lease-set"
            or plan.get("writable_root_policy") != "vm-specific"
        ):
            raise StateError("OCI-root preparation boot plan policy is invalid")
        run = plan.get("run")
        if not isinstance(run, Mapping) or set(run) != {"backend", "name", "run_id", "runtime_kind"}:
            raise StateError("OCI-root preparation run binding is invalid")
        _validate_size(self.volume_size_bytes)
        if self.retention_policy not in {"delete", "retain"}:
            raise StateError("OCI-root preparation retention policy is invalid")
        if self.rollback_action not in _ROLLBACK_ACTIONS:
            raise StateError("OCI-root preparation rollback action is invalid")
        if self.rollback_action == "retain" and self.retention_policy != "retain":
            raise StateError("OCI-root retained reuse must preserve retention")
        try:
            parsed_volume_id = uuid.UUID(self.volume_id)
        except (AttributeError, TypeError, ValueError):
            raise StateError("OCI-root preparation volume ID is invalid") from None
        if str(parsed_volume_id) != self.volume_id:
            raise StateError("OCI-root preparation volume ID is not canonical")
        object.__setattr__(self, "boot_plan", plan)
        object.__setattr__(self, "boot_plan_digest", plan_digest)
        object.__setattr__(self, "lower_lease_set_id", lower_set)
        object.__setattr__(self, "lower_graph_digest", lower_graph)

    @property
    def owner(self) -> ArtifactLeaseOwner:
        run = self.boot_plan["run"]
        assert isinstance(run, Mapping)
        if run.get("runtime_kind") != "oci-root" or run.get("backend") != "kvm":
            raise StateError("OCI-root preparation dispatch binding is invalid")
        return ArtifactLeaseOwner(str(run.get("run_id", "")), str(run.get("name", "")), "root-lower")

    @property
    def receipts(self) -> tuple[DerivedLayerReceipt, ...]:
        layers = self.boot_plan.get("layers")
        if not isinstance(layers, list):
            raise StateError("OCI-root preparation layers are invalid")
        receipts: list[DerivedLayerReceipt] = []
        for ordinal, raw_layer in enumerate(layers):
            if not isinstance(raw_layer, Mapping) or set(raw_layer) != {
                "compressed",
                "derived_receipt",
                "diff_id",
                "ordinal",
            }:
                raise StateError("OCI-root preparation layer fields are invalid")
            if raw_layer.get("ordinal") != ordinal:
                raise StateError("OCI-root preparation layer order is invalid")
            try:
                receipts.append(DerivedLayerReceipt.from_dict(raw_layer.get("derived_receipt")))
            except OCIStoreError:
                raise StateError("OCI-root preparation layer receipt is invalid") from None
        return tuple(receipts)

    @property
    def occurrences(self) -> tuple[DerivedLayerOccurrence, ...]:
        layers = self.boot_plan["layers"]
        assert isinstance(layers, list)
        occurrences: list[DerivedLayerOccurrence] = []
        for ordinal, raw_layer in enumerate(layers):
            assert isinstance(raw_layer, Mapping)
            compressed = raw_layer.get("compressed")
            if not isinstance(compressed, Mapping) or set(compressed) != {"digest", "mediaType", "size"}:
                raise StateError("OCI-root preparation compressed descriptor is invalid")
            try:
                occurrences.append(
                    DerivedLayerOccurrence(
                        source_snapshot_binding_digest=str(self.boot_plan["source_snapshot_binding_digest"]),
                        source_image_digest=str(self.boot_plan["source_image_digest"]),
                        ordinal=ordinal,
                        media_type=compressed["mediaType"],
                        compressed_digest=compressed["digest"],
                        compressed_size=compressed["size"],
                        diff_id=raw_layer["diff_id"],
                    )
                )
            except (KeyError, OCIStoreError, TypeError, ValueError):
                raise StateError("OCI-root preparation occurrence is invalid") from None
        return tuple(occurrences)

    def to_dict(self) -> dict[str, Any]:
        return {
            "boot_plan": _plain_json(self.boot_plan),
            "boot_plan_digest": self.boot_plan_digest,
            "lower_lease_set_id": self.lower_lease_set_id,
            "phase": self.phase,
            "root_volume": {
                "backend": "kvm",
                "filesystem": "ext4",
                "lower_graph_digest": self.lower_graph_digest,
                "retention_policy": self.retention_policy,
                "rollback_action": self.rollback_action,
                "size_bytes": self.volume_size_bytes,
                "volume_id": self.volume_id,
            },
            "schema": OCI_ROOT_PREPARATION_SCHEMA,
        }

    @classmethod
    def from_dict(cls, value: Any) -> OCIRootPreparationTransaction:
        if not isinstance(value, Mapping) or set(value) != {
            "boot_plan",
            "boot_plan_digest",
            "lower_lease_set_id",
            "phase",
            "root_volume",
            "schema",
        }:
            raise StateError("OCI-root preparation ledger fields are invalid")
        if value.get("schema") != OCI_ROOT_PREPARATION_SCHEMA:
            raise StateError("OCI-root preparation ledger schema is invalid")
        volume = value.get("root_volume")
        if not isinstance(volume, Mapping) or set(volume) != {
            "backend",
            "filesystem",
            "lower_graph_digest",
            "retention_policy",
            "rollback_action",
            "size_bytes",
            "volume_id",
        }:
            raise StateError("OCI-root preparation volume fields are invalid")
        if volume.get("backend") != "kvm" or volume.get("filesystem") != "ext4":
            raise StateError("OCI-root preparation volume backend is invalid")
        try:
            transaction = cls(
                phase=value["phase"],
                boot_plan=value["boot_plan"],
                boot_plan_digest=value["boot_plan_digest"],
                lower_lease_set_id=value["lower_lease_set_id"],
                volume_id=volume["volume_id"],
                volume_size_bytes=volume["size_bytes"],
                lower_graph_digest=volume["lower_graph_digest"],
                retention_policy=volume["retention_policy"],
                rollback_action=volume["rollback_action"],
            )
        except (KeyError, TypeError, ValueError):
            raise StateError("OCI-root preparation ledger is invalid") from None
        if transaction.to_dict() != _plain_json(value):
            raise StateError("OCI-root preparation ledger is not canonical")
        return transaction


@dataclass(frozen=True, slots=True)
class PreparedOCIRootRun:
    boot_plan: PreparedOCIBootPlan
    root_volume: ClaimedOCIRootVolume
    transaction: OCIRootPreparationTransaction

    def __post_init__(self) -> None:
        if self.transaction.phase != "resources-ready":
            raise StateError("prepared OCI-root run is not ready")
        if (
            self.boot_plan.intent.digest != self.transaction.boot_plan_digest
            or self.boot_plan.lower_leases.lease_set_id != self.transaction.lower_lease_set_id
            or self.root_volume.record.volume_id != self.transaction.volume_id
            or self.root_volume.record.lower_graph_digest != self.transaction.lower_graph_digest
            or self.boot_plan.intent.owner != self.transaction.owner
        ):
            raise StateError("prepared OCI-root run binding is invalid")


@dataclass(frozen=True, slots=True)
class ReconciledOCIRootPreparation:
    transaction: OCIRootPreparationTransaction
    lower_leases: DurableLeaseSet | None
    root_volume: OCIRootVolumeRecord | None


def _validate_reservation(reservation: NewRunReservation) -> ArtifactLeaseOwner:
    if not isinstance(reservation, NewRunReservation):
        raise StateError("OCI-root preparation requires a new-run reservation")
    if (
        reservation.dispatch_key.runtime_kind is not RuntimeKind.OCI_ROOT
        or reservation.dispatch_key.backend is not RuntimeBackend.KVM
    ):
        raise StateError("OCI-root preparation requires the OCI-root/KVM dispatch")
    reservation.verify_binding()
    return ArtifactLeaseOwner(reservation.record.run_id, reservation.record.name, "root-lower")


def _ledger_data(transaction: OCIRootPreparationTransaction, *, created_at: str) -> dict[str, Any]:
    return {"created_at": created_at, "oci_root": transaction.to_dict()}


def _rollback_lower(transaction: OCIRootPreparationTransaction, store: OCIStore) -> None:
    try:
        store.rollback_lease_set(
            transaction.lower_lease_set_id,
            transaction.owner,
            plan_digest=transaction.boot_plan_digest,
        )
    except OCIStoreError as exc:
        if exc.code != "oci-store-missing":
            raise


def prepare_oci_root_run(
    reservation: NewRunReservation,
    materialization: OCIImageMaterializationReceipt,
    store: OCIStore,
    *,
    root_volume_size_bytes: int,
    retained_volume_id: str | None = None,
    retention_policy: str = "delete",
    runner: CommandRunner = _default_runner,
) -> PreparedOCIRootRun:
    """Durably prepare immutable lowers and one exclusive writable root."""
    owner = _validate_reservation(reservation)
    if not isinstance(store, OCIStore):
        raise StateError("OCI-root preparation store is invalid")
    size_bytes = _validate_size(root_volume_size_bytes)
    intent = OCIBootPlanIntent(owner.run_id, owner.run_name, materialization)
    store.validate_receipt_occurrences(intent.receipts, intent.occurrences)
    lease_set_id = store.lease_set_id(intent.receipts, owner, plan_digest=intent.digest)
    rollback_action = "retain" if retained_volume_id is not None else "delete"
    volume_id = retained_volume_id or new_oci_root_volume_id()
    if retained_volume_id is not None:
        retained = load_oci_root_volume(reservation.roots, volume_id, runner=runner).record
        if (
            retained.status != "retained"
            or retained.size_bytes != size_bytes
            or retained.lower_graph_digest != intent.lower_graph_digest
            or retained.retention_policy != "retain"
            or retention_policy != "retain"
        ):
            raise StateError("retained OCI-root volume does not match the requested root")
    transaction = OCIRootPreparationTransaction(
        "resources-planned",
        intent.to_dict(),
        intent.digest,
        lease_set_id,
        volume_id,
        size_bytes,
        intent.lower_graph_digest,
        retention_policy,
        rollback_action,
    )
    created_at = utc_now_iso()
    reservation.write_state("creating", _ledger_data(transaction, created_at=created_at))
    prepared: PreparedOCIBootPlan | None = None
    claimed: ClaimedOCIRootVolume | None = None
    try:
        prepared = prepare_oci_boot_plan(
            materialization,
            run_id=owner.run_id,
            run_name=owner.run_name,
            store=store,
        )
        claimed = claim_oci_root_volume(
            reservation.roots,
            volume_id,
            size_bytes=size_bytes,
            lower_graph_digest=intent.lower_graph_digest,
            retention_policy=retention_policy,
            owner=owner,
            runner=runner,
        )
        if (rollback_action == "delete" and not claimed.created) or (
            rollback_action == "retain" and not claimed.claimed_from_retained
        ):
            raise StateError("OCI-root volume claim provenance does not match the preparation intent")
        ready = replace(transaction, phase="resources-ready")
        reservation.write_state("creating", _ledger_data(ready, created_at=created_at))
        return PreparedOCIRootRun(prepared, claimed, ready)
    except BaseException:
        rollback_error: BaseException | None = None
        if claimed is not None:
            try:
                rollback_oci_root_volume_claim(reservation.roots, claimed, owner=owner, runner=runner)
            except BaseException as exc:
                rollback_error = exc
        try:
            _rollback_lower(transaction, store)
        except BaseException as exc:
            rollback_error = rollback_error or exc
        phase = "rollback-required" if rollback_error is not None else "rolled-back"
        try:
            reservation.write_failure(
                {
                    **_ledger_data(replace(transaction, phase=phase), created_at=created_at),
                    "error": "OCI-root resource preparation failed",
                }
            )
        except BaseException:
            pass
        if rollback_error is not None:
            raise StateError("OCI-root preparation failed and rollback is incomplete") from rollback_error
        raise


def release_prepared_oci_root_run(
    roots: StatePaths,
    prepared: PreparedOCIRootRun,
    store: OCIStore,
    *,
    runner: CommandRunner = _default_runner,
) -> None:
    """Durably release a prepared run, retaining its root only by policy."""
    if not isinstance(prepared, PreparedOCIRootRun):
        raise StateError("prepared OCI-root release input is invalid")
    snapshot = read_run_ledger_snapshot(roots, prepared.transaction.owner.run_name)
    current = _transaction_from_snapshot(snapshot)
    if current != prepared.transaction:
        raise StateError("prepared OCI-root release ledger changed")
    releasing = replace(current, phase="release-required")
    with locked_existing_run(
        roots,
        current.owner.run_name,
        expected_snapshot=snapshot,
    ) as mutation:
        data = mutation.mutable_state()
        created_at = data.get("created_at")
        if not isinstance(created_at, str):
            raise StateError("OCI-root preparation creation time is invalid")
        mutation.write_state("removing", _ledger_data(releasing, created_at=created_at))
    _finish_release(roots, releasing, store, runner=runner)
    after_release = read_run_ledger_snapshot(roots, current.owner.run_name)
    _commit_released_ledger(roots, after_release, releasing)


def _finish_release(
    roots: StatePaths,
    transaction: OCIRootPreparationTransaction,
    store: OCIStore,
    *,
    runner: CommandRunner,
) -> None:
    try:
        volume = load_oci_root_volume(roots, transaction.volume_id, runner=runner).record
    except StateError as exc:
        if str(exc) == "OCI-root volume record is missing" and transaction.retention_policy == "delete":
            volume = None
        elif str(exc) == "OCI-root volume lifecycle is incomplete":
            release_oci_root_volume(
                roots,
                transaction.volume_id,
                owner=transaction.owner,
                lower_graph_digest=transaction.lower_graph_digest,
                delete=True,
                runner=runner,
            )
            volume = None
        else:
            raise
    if volume is not None:
        if volume.status == "retained" and transaction.retention_policy == "retain":
            pass
        elif volume.status == "attached":
            release_oci_root_volume(
                roots,
                transaction.volume_id,
                owner=transaction.owner,
                lower_graph_digest=transaction.lower_graph_digest,
                runner=runner,
            )
        else:
            raise StateError("OCI-root release volume binding is invalid")
    _rollback_lower(transaction, store)


def _commit_released_ledger(
    roots: StatePaths,
    snapshot: RunLedgerSnapshot,
    transaction: OCIRootPreparationTransaction,
) -> OCIRootPreparationTransaction:
    current = _transaction_from_snapshot(snapshot)
    if current != transaction:
        raise StateError("OCI-root release ledger changed")
    released = replace(transaction, phase="released")
    with locked_existing_run(roots, transaction.owner.run_name, expected_snapshot=snapshot) as mutation:
        data = mutation.mutable_state()
        created_at = data.get("created_at")
        if not isinstance(created_at, str):
            raise StateError("OCI-root preparation creation time is invalid")
        mutation.write_state("removed", _ledger_data(released, created_at=created_at))
    return released


def _transaction_from_snapshot(snapshot: RunLedgerSnapshot) -> OCIRootPreparationTransaction:
    if (
        snapshot.record.dispatch_key.runtime_kind is not RuntimeKind.OCI_ROOT
        or snapshot.record.dispatch_key.backend is not RuntimeBackend.KVM
    ):
        raise StateError("run is not an OCI-root/KVM run")
    transaction = OCIRootPreparationTransaction.from_dict(snapshot.state.get("oci_root"))
    if transaction.owner.run_id != snapshot.record.run_id or transaction.owner.run_name != snapshot.record.name:
        raise StateError("OCI-root preparation owner does not match the run ledger")
    return transaction


def reconcile_oci_root_preparation(
    roots: StatePaths,
    name: str,
    store: OCIStore,
    *,
    runner: CommandRunner = _default_runner,
) -> ReconciledOCIRootPreparation:
    """Recover a ready transaction or roll back its exact planned resources."""
    snapshot = read_run_ledger_snapshot(roots, name)
    transaction = _transaction_from_snapshot(snapshot)
    if transaction.phase == "release-required":
        _finish_release(roots, transaction, store, runner=runner)
        after_release = read_run_ledger_snapshot(roots, name)
        released = _commit_released_ledger(roots, after_release, transaction)
        volume = None
        if transaction.retention_policy == "retain":
            volume = load_oci_root_volume(roots, transaction.volume_id, runner=runner).record
            if volume.status != "retained":
                raise StateError("released OCI-root retained volume is invalid")
        return ReconciledOCIRootPreparation(released, None, volume)
    if transaction.phase == "released":
        volume = None
        if transaction.retention_policy == "retain":
            volume = load_oci_root_volume(roots, transaction.volume_id, runner=runner).record
        return ReconciledOCIRootPreparation(transaction, None, volume)
    if transaction.phase == "resources-ready":
        store.validate_receipt_occurrences(transaction.receipts, transaction.occurrences)
        leases = store.load_lease_set(
            transaction.lower_lease_set_id,
            transaction.owner,
            plan_digest=transaction.boot_plan_digest,
        )
        if tuple(member.receipt for member in leases.members) != transaction.receipts:
            raise StateError("ready OCI-root lower lease binding is invalid")
        volume = load_oci_root_volume(roots, transaction.volume_id, runner=runner).record
        if (
            volume.status != "attached"
            or volume.attached_run_id != transaction.owner.run_id
            or volume.attached_run_name != transaction.owner.run_name
            or volume.size_bytes != transaction.volume_size_bytes
            or volume.lower_graph_digest != transaction.lower_graph_digest
            or volume.retention_policy != transaction.retention_policy
        ):
            raise StateError("ready OCI-root volume binding is invalid")
        return ReconciledOCIRootPreparation(transaction, leases, volume)
    if transaction.phase == "rolled-back":
        return ReconciledOCIRootPreparation(transaction, None, None)

    try:
        verified = load_oci_root_volume(roots, transaction.volume_id, runner=runner)
    except StateError as exc:
        if str(exc) == "OCI-root volume lifecycle is incomplete" and transaction.rollback_action == "delete":
            release_oci_root_volume(
                roots,
                transaction.volume_id,
                owner=transaction.owner,
                lower_graph_digest=transaction.lower_graph_digest,
                delete=True,
                runner=runner,
            )
        elif str(exc) != "OCI-root volume record is missing":
            raise
    else:
        volume = verified.record
        if volume.status == "attached":
            release_oci_root_volume(
                roots,
                transaction.volume_id,
                owner=transaction.owner,
                lower_graph_digest=transaction.lower_graph_digest,
                delete=transaction.rollback_action == "delete",
                runner=runner,
            )
        elif not (volume.status == "retained" and transaction.rollback_action == "retain"):
            raise StateError("planned OCI-root volume is owned by another lifecycle")
    _rollback_lower(transaction, store)
    rolled_back = replace(transaction, phase="rolled-back")
    with locked_existing_run(roots, name, expected_snapshot=snapshot) as mutation:
        current = mutation.mutable_state()
        created_at = current.get("created_at")
        if not isinstance(created_at, str):
            raise StateError("OCI-root preparation creation time is invalid")
        mutation.write_state(
            "failed",
            {
                "created_at": created_at,
                "error": "interrupted OCI-root preparation was rolled back",
                "oci_root": rolled_back.to_dict(),
            },
        )
    return ReconciledOCIRootPreparation(rolled_back, None, None)


__all__ = [
    "OCI_ROOT_PREPARATION_SCHEMA",
    "OCIRootPreparationTransaction",
    "PreparedOCIRootRun",
    "ReconciledOCIRootPreparation",
    "prepare_oci_root_run",
    "reconcile_oci_root_preparation",
    "release_prepared_oci_root_run",
]
