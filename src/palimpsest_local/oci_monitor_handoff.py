"""Retire old lower pins only after a prepared successor owns replacement pins.

This private transition never alters root bytes, attachments, domains, sockets,
or lifecycle status. Intent is durable before store locks; completion is written
after those locks are released, avoiding artifact-reference/digest inversion.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Any

from .errors import StateError
from .oci_monitor import MonitorProcessIdentity, ProcessLiveness, probe_process_liveness
from .oci_monitor_ipc import MonitorPreActivationBinding
from .oci_monitor_recovery import (
    _DIGEST,
    MonitorInactiveCleanupReceipt,
    _inspect_domain,
    _lookup,
    _RecoveryAuthority,
    _stale,
    _uuid,
    _validate_ledger,
)
from .oci_monitor_retention import MonitorRootRetentionReceipt, _digest, _resource_bindings
from .oci_root_prepare import OCIRootPreparationTransaction
from .oci_root_volume import _locked_exact_root_volume
from .oci_store import ArtifactLeaseOwner, DerivedLayerReceipt, DurableLeaseSet, DurableLeaseSetMember, OCIStore
from .project_volumes import CommandRunner, _default_runner
from .runtime_types import DispatchKey, ExistingRunRecord, RuntimeBackend, RuntimeKind
from .state import StatePaths, locked_existing_run

_SCHEMA = "palimpsest.oci-monitor-lower-handoff.v1"
_STATE_KEY = "oci_monitor_lower_handoff"


class MonitorLowerHandoffError(StateError):
    """Path-free refusal or interrupted lower-lease handoff."""


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _lease_snapshot(leases: DurableLeaseSet) -> dict[str, Any]:
    return {
        "lease_set_id": leases.lease_set_id,
        "plan_digest": leases.plan_digest,
        "owner": leases.owner.to_dict(),
        "members": [
            {
                "ordinal": member.ordinal,
                "lease_id": member.lease_id,
                "acquired_ns": member.acquired_ns,
                "receipt": member.receipt.to_dict(),
            }
            for member in leases.members
        ],
    }


def _parse_leases(value: object) -> DurableLeaseSet:
    raw = _plain(value)
    if type(raw) is not dict or set(raw) != {"lease_set_id", "plan_digest", "owner", "members"}:
        raise MonitorLowerHandoffError("invalid handoff lease snapshot")
    if (
        type(raw["owner"]) is not dict
        or set(raw["owner"]) != {"run_id", "run_name", "role"}
        or type(raw["members"]) is not list
    ):
        raise MonitorLowerHandoffError("invalid handoff lease snapshot")
    members = []
    for member in raw["members"]:
        if type(member) is not dict or set(member) != {"ordinal", "lease_id", "acquired_ns", "receipt"}:
            raise MonitorLowerHandoffError("invalid handoff lease snapshot")
        members.append(
            DurableLeaseSetMember(
                member["ordinal"],
                member["lease_id"],
                DerivedLayerReceipt.from_dict(member["receipt"]),
                member["acquired_ns"],
            )
        )
    result = DurableLeaseSet(
        raw["lease_set_id"], raw["plan_digest"], ArtifactLeaseOwner(**raw["owner"]), tuple(members)
    )
    if _lease_snapshot(result) != raw:
        raise MonitorLowerHandoffError("noncanonical handoff lease snapshot")
    return result


def _record_dict(record: ExistingRunRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "run_id": record.run_id,
        "schema_version": record.state_schema_version,
        "runtime_kind": record.dispatch_key.runtime_kind.value,
        "backend": record.dispatch_key.backend.value,
    }


def _parse_record(value: object) -> ExistingRunRecord:
    raw = _plain(value)
    if type(raw) is not dict or set(raw) != {"name", "run_id", "schema_version", "runtime_kind", "backend"}:
        raise MonitorLowerHandoffError("invalid handoff successor record")
    record = ExistingRunRecord(
        raw["name"],
        raw["run_id"],
        raw["schema_version"],
        DispatchKey(RuntimeKind(raw["runtime_kind"]), RuntimeBackend(raw["backend"])),
    )
    if record.state_schema_version != 2 or record.dispatch_key != DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM):
        raise MonitorLowerHandoffError("invalid handoff successor record")
    return record


@dataclass(frozen=True, slots=True)
class MonitorLowerHandoffReceipt:
    handoff_id: str
    phase: str
    retention: MonitorRootRetentionReceipt
    successor: ExistingRunRecord
    successor_transaction_digest: str
    source_leases: DurableLeaseSet
    successor_leases: DurableLeaseSet

    def __post_init__(self) -> None:
        if (
            not _uuid(self.handoff_id)
            or type(self.phase) is not str
            or self.phase not in {"intent", "completed"}
            or type(self.retention) is not MonitorRootRetentionReceipt
            or self.retention.phase != "completed"
            or type(self.successor) is not ExistingRunRecord
            or type(self.successor_transaction_digest) is not str
            or _DIGEST.fullmatch(self.successor_transaction_digest) is None
            or type(self.source_leases) is not DurableLeaseSet
            or type(self.successor_leases) is not DurableLeaseSet
        ):
            raise MonitorLowerHandoffError("invalid lower handoff receipt")
        self.retention.__post_init__()
        _parse_record(_record_dict(self.successor))
        _parse_leases(_lease_snapshot(self.source_leases))
        _parse_leases(_lease_snapshot(self.successor_leases))
        old, new = self.source_leases, self.successor_leases
        if (
            old.owner
            != ArtifactLeaseOwner(
                self.retention.source_volume.attached_run_id,
                self.retention.source_volume.attached_run_name,
                "root-lower",
            )
            or old.lease_set_id != self.retention.lower_lease_set_id
            or new.owner != ArtifactLeaseOwner(self.successor.run_id, self.successor.name, "root-lower")
            or old.owner.run_id == new.owner.run_id
            or old.owner.run_name == new.owner.run_name
            or old.lease_set_id == new.lease_set_id
            or tuple(member.receipt for member in old.members) != tuple(member.receipt for member in new.members)
            or set(member.lease_id for member in old.members) & set(member.lease_id for member in new.members)
        ):
            raise MonitorLowerHandoffError("lower handoff source and successor bindings disagree")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "handoff_id": self.handoff_id,
            "phase": self.phase,
            "retention": self.retention.to_dict(),
            "successor": _record_dict(self.successor),
            "successor_transaction_digest": self.successor_transaction_digest,
            "source_leases": _lease_snapshot(self.source_leases),
            "successor_leases": _lease_snapshot(self.successor_leases),
        }

    @classmethod
    def from_dict(cls, value: object) -> MonitorLowerHandoffReceipt:
        try:
            raw = _plain(value)
            if (
                type(raw) is not dict
                or set(raw)
                != {
                    "schema",
                    "handoff_id",
                    "phase",
                    "retention",
                    "successor",
                    "successor_transaction_digest",
                    "source_leases",
                    "successor_leases",
                }
                or raw["schema"] != _SCHEMA
            ):
                raise ValueError
            return cls(
                raw["handoff_id"],
                raw["phase"],
                MonitorRootRetentionReceipt.from_dict(raw["retention"]),
                _parse_record(raw["successor"]),
                raw["successor_transaction_digest"],
                _parse_leases(raw["source_leases"]),
                _parse_leases(raw["successor_leases"]),
            )
        except Exception:
            raise MonitorLowerHandoffError("invalid lower handoff receipt") from None


def _source_context(mutation, binding, authority):
    journal = authority.snapshot
    cleanup = MonitorInactiveCleanupReceipt.from_dict(mutation.snapshot.state.get("oci_monitor_inactive_cleanup"))
    expected = MonitorInactiveCleanupReceipt(
        binding,
        cleanup.cleanup_id,
        "sha256:" + hashlib.sha256(authority.content).hexdigest(),
        journal.identity.generation,
        journal.revision,
        authority.journal_metadata.st_dev,
        authority.journal_metadata.st_ino,
        journal.writer,
        "completed",
    )
    transaction, plan, source = _resource_bindings(mutation.snapshot.state, binding)
    retention = MonitorRootRetentionReceipt.from_dict(mutation.snapshot.state.get("oci_monitor_root_retention"))
    if (
        cleanup != expected
        or retention.phase != "completed"
        or (
            retention.binding_digest != binding.digest
            or retention.cleanup_receipt_digest != _digest(cleanup.to_dict())
            or retention.transaction_digest != _digest(transaction.to_dict())
            or retention.lower_lease_set_id != transaction.lower_lease_set_id
            or retention.source_volume != source
            or retention.filesystem_uuid != plan.root_volume["filesystem_uuid"]
        )
    ):
        raise MonitorLowerHandoffError("lower handoff source cleanup or retention evidence changed")
    return cleanup, retention, transaction, plan, source


def _successor_transaction(mutation, record, source_transaction):
    state = mutation.snapshot.state
    if (
        mutation.record != record
        or state.get("status") != "creating"
        or any(
            key.startswith("oci_monitor") or key in {"oci_root_domain", "oci_root_definition", "oci_root_handoff"}
            for key in state
        )
    ):
        raise MonitorLowerHandoffError("lower handoff successor must be prepared before definition")
    transaction = OCIRootPreparationTransaction.from_dict(state.get("oci_root"))
    if (
        transaction.phase != "resources-ready"
        or transaction.retention_policy != "retain"
        or transaction.rollback_action != "retain"
        or transaction.owner != ArtifactLeaseOwner(record.run_id, record.name, "root-lower")
        or transaction.volume_id != source_transaction.volume_id
        or transaction.volume_size_bytes != source_transaction.volume_size_bytes
        or transaction.lower_graph_digest != source_transaction.lower_graph_digest
        or transaction.receipts != source_transaction.receipts
        or transaction.lower_lease_set_id == source_transaction.lower_lease_set_id
    ):
        raise MonitorLowerHandoffError("lower handoff successor resources disagree")
    return transaction


def _validate_lease_binding(leases, transaction, store):
    if (
        leases.owner != transaction.owner
        or leases.lease_set_id != transaction.lower_lease_set_id
        or leases.plan_digest != transaction.boot_plan_digest
        or tuple(member.receipt for member in leases.members) != transaction.receipts
        or any(member.receipt.store_id != store.identity for member in leases.members)
    ):
        raise MonitorLowerHandoffError("lower handoff lease snapshot binding changed")


def handoff_retained_root_lower_leases(
    roots: StatePaths,
    source_binding: MonitorPreActivationBinding,
    successor_record: ExistingRunRecord,
    store: OCIStore,
    *,
    conn: Any,
    runner: CommandRunner = _default_runner,
    liveness_probe: Callable[[MonitorProcessIdentity], ProcessLiveness] = probe_process_liveness,
) -> MonitorLowerHandoffReceipt:
    """Transfer durable lower retention to an explicit, not-yet-defined successor."""
    authority = None
    try:
        if (
            type(roots) is not StatePaths
            or type(source_binding) is not MonitorPreActivationBinding
            or type(successor_record) is not ExistingRunRecord
            or type(store) is not OCIStore
            or not callable(runner)
            or not callable(liveness_probe)
            or store._root != roots.oci_derived_store.resolve()
        ):
            raise MonitorLowerHandoffError("invalid lower handoff authority")
        source_binding.__post_init__()
        _parse_record(_record_dict(successor_record))
        if (
            source_binding.owner_uid != os.geteuid()
            or source_binding.record.name == successor_record.name
            or source_binding.record.run_id == successor_record.run_id
        ):
            raise MonitorLowerHandoffError("lower handoff requires distinct source and successor")

        def verify_source(mutation, context):
            authority.validate()
            _validate_ledger(mutation, source_binding, authority.snapshot)
            if _source_context(mutation, source_binding, authority) != context:
                raise MonitorLowerHandoffError("lower handoff source evidence changed")
            _stale(authority.snapshot, liveness_probe)
            if _inspect_domain(conn, source_binding) is not None:
                raise MonitorLowerHandoffError("lower handoff source domain reappeared")
            authority.validate()
            _stale(authority.snapshot, liveness_probe)
            if conn.getURI() != source_binding.libvirt_uri:
                raise MonitorLowerHandoffError("lower handoff connection URI changed")

        # Historical completion does not require a surviving/prepared target.
        with locked_existing_run(roots, source_binding.record.name, expected=source_binding.record) as source_mutation:
            saved = source_mutation.snapshot.state.get(_STATE_KEY)
            if _STATE_KEY in source_mutation.snapshot.state:
                receipt = MonitorLowerHandoffReceipt.from_dict(saved)
                if receipt.phase == "completed":
                    authority = _RecoveryAuthority(source_mutation, source_binding)
                    context = _source_context(source_mutation, source_binding, authority)
                    verify_source(source_mutation, context)
                    if receipt.retention != context[1] or receipt.successor != successor_record:
                        raise MonitorLowerHandoffError("lower handoff completed binding changed")
                    _validate_lease_binding(receipt.source_leases, context[2], store)
                    store._validate_handoff_lease_set(receipt.source_leases)
                    store._validate_handoff_lease_set(receipt.successor_leases)
                    store._require_lease_set_retired(receipt.source_leases)
                    verify_source(source_mutation, context)
                    return receipt

        with ExitStack() as stack:
            mutations = {}
            for record in sorted((source_binding.record, successor_record), key=lambda item: item.name):
                mutations[record.name] = stack.enter_context(locked_existing_run(roots, record.name, expected=record))
            source_mutation, successor_mutation = (
                mutations[source_binding.record.name],
                mutations[successor_record.name],
            )
            authority = _RecoveryAuthority(source_mutation, source_binding)
            context = _source_context(source_mutation, source_binding, authority)
            _, retention, source_transaction, _, source_volume = context
            successor_transaction = _successor_transaction(successor_mutation, successor_record, source_transaction)
            saved = _STATE_KEY in source_mutation.snapshot.state
            receipt = (
                MonitorLowerHandoffReceipt.from_dict(source_mutation.snapshot.state[_STATE_KEY]) if saved else None
            )
            if receipt is not None and receipt.phase == "completed":
                raise MonitorLowerHandoffError("lower handoff completed concurrently; retry historical lookup")
            source_leases = (
                receipt.source_leases
                if receipt
                else store.load_lease_set(
                    source_transaction.lower_lease_set_id,
                    source_transaction.owner,
                    plan_digest=source_transaction.boot_plan_digest,
                )
            )
            successor_leases = store.load_lease_set(
                successor_transaction.lower_lease_set_id,
                successor_transaction.owner,
                plan_digest=successor_transaction.boot_plan_digest,
            )
            _validate_lease_binding(source_leases, source_transaction, store)
            _validate_lease_binding(successor_leases, successor_transaction, store)
            store._validate_handoff_lease_set(source_leases)
            store._validate_handoff_lease_set(successor_leases)
            expected = MonitorLowerHandoffReceipt(
                receipt.handoff_id if receipt else str(uuid.uuid4()),
                "intent",
                retention,
                successor_record,
                _digest(successor_transaction.to_dict()),
                source_leases,
                successor_leases,
            )
            if receipt is not None and receipt != expected:
                raise MonitorLowerHandoffError("lower handoff intent evidence changed")
            receipt = expected
            successor_volume = replace(
                source_volume,
                attached_run_id=successor_record.run_id,
                attached_run_name=successor_record.name,
                generation=retention.retained_generation + 1,
            )

            with _locked_exact_root_volume(
                roots, successor_volume, filesystem_uuid=retention.filesystem_uuid, runner=runner
            ) as (current, identity, verify_volume, _fd, _path):
                if identity != (retention.root_device, retention.root_inode):
                    raise MonitorLowerHandoffError("lower handoff root data identity changed")

                def verify():
                    verify_source(source_mutation, context)
                    successor_mutation.verify_binding()
                    if (
                        _successor_transaction(successor_mutation, successor_record, source_transaction)
                        != successor_transaction
                    ):
                        raise MonitorLowerHandoffError("lower handoff successor changed")
                    if _lookup(conn, "lookupByName", successor_record.name) is not None:
                        raise MonitorLowerHandoffError("lower handoff successor domain already exists")
                    successor_mutation.verify_binding()
                    authority.validate()
                    _stale(authority.snapshot, liveness_probe)
                    if conn.getURI() != source_binding.libvirt_uri:
                        raise MonitorLowerHandoffError("lower handoff connection URI changed")
                    verify_volume(current)

                verify()
                if not saved:
                    data = source_mutation.mutable_state()
                    data[_STATE_KEY] = receipt.to_dict()
                    verify()
                    source_mutation.write_state(data["status"], data)
                    verify()
                store._retire_replaced_lease_set(source_leases, successor_leases, resume=saved, verify=verify)
                verify()
                receipt = replace(receipt, phase="completed")
                data = source_mutation.mutable_state()
                data[_STATE_KEY] = receipt.to_dict()
                verify()
                source_mutation.write_state(data["status"], data)
                verify()
                return receipt
    except MonitorLowerHandoffError:
        raise
    except Exception:
        raise MonitorLowerHandoffError("retained root lower handoff could not be verified") from None
    finally:
        if authority is not None:
            authority.close()


__all__ = ["MonitorLowerHandoffError", "MonitorLowerHandoffReceipt", "handoff_retained_root_lower_leases"]
