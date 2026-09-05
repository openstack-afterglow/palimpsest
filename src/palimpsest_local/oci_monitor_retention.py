"""Private post-cleanup root detachment, preserving the original lower leases.

A completed receipt is historical evidence of detachment, not a statement
about the volume's current owner. Replaying it never opens the volume.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .errors import StateError
from .oci_monitor import MonitorProcessIdentity, ProcessLiveness, probe_process_liveness
from .oci_monitor_ipc import MonitorPreActivationBinding, _canonical_bytes
from .oci_monitor_recovery import (
    _DIGEST,
    MonitorInactiveCleanupReceipt,
    _inspect_domain,
    _RecoveryAuthority,
    _stale,
    _uuid,
    _validate_ledger,
)
from .oci_root_kvm import OCIRootDomainPlan
from .oci_root_prepare import OCIRootPreparationTransaction
from .oci_root_volume import OCIRootVolumeRecord, _retain_exact_oci_root_volume
from .oci_store import OCIStore
from .project_volumes import CommandRunner, _default_runner
from .state import StatePaths, locked_existing_run

_SCHEMA = "palimpsest.oci-monitor-root-retention.v1"
_STATE_KEY = "oci_monitor_root_retention"


class MonitorRootRetentionError(StateError):
    """Path-free refusal or incomplete retained-root transition."""


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class MonitorRootRetentionReceipt:
    retention_id: str
    phase: str
    binding_digest: str
    cleanup_receipt_digest: str
    transaction_digest: str
    lower_lease_set_id: str
    source_volume: OCIRootVolumeRecord
    filesystem_uuid: str
    root_device: int
    root_inode: int

    def __post_init__(self) -> None:
        if (
            not _uuid(self.retention_id)
            or not _uuid(self.filesystem_uuid)
            or type(self.phase) is not str
            or self.phase not in {"intent", "completed"}
            or any(
                type(value) is not str or _DIGEST.fullmatch(value) is None
                for value in (
                    self.binding_digest,
                    self.cleanup_receipt_digest,
                    self.transaction_digest,
                    self.lower_lease_set_id,
                )
            )
            or type(self.source_volume) is not OCIRootVolumeRecord
            or self.source_volume.status != "attached"
            or self.source_volume.retention_policy != "retain"
            or type(self.root_device) is not int
            or self.root_device < 0
            or type(self.root_inode) is not int
            or self.root_inode <= 0
        ):
            raise MonitorRootRetentionError("invalid root retention receipt")
        self.source_volume.__post_init__()
        replace(self.source_volume, generation=self.retained_generation)

    @property
    def retained_generation(self) -> int:
        return self.source_volume.generation + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "retention_id": self.retention_id,
            "phase": self.phase,
            "binding_digest": self.binding_digest,
            "cleanup_receipt_digest": self.cleanup_receipt_digest,
            "transaction_digest": self.transaction_digest,
            "lower_lease_set_id": self.lower_lease_set_id,
            "source_volume": self.source_volume.to_dict(),
            "retained_generation": self.retained_generation,
            "filesystem_uuid": self.filesystem_uuid,
            "root_device": self.root_device,
            "root_inode": self.root_inode,
        }

    @classmethod
    def from_dict(cls, value: object) -> MonitorRootRetentionReceipt:
        try:
            if (
                not isinstance(value, Mapping)
                or set(value)
                != {
                    "schema",
                    "retention_id",
                    "phase",
                    "binding_digest",
                    "cleanup_receipt_digest",
                    "transaction_digest",
                    "lower_lease_set_id",
                    "source_volume",
                    "retained_generation",
                    "filesystem_uuid",
                    "root_device",
                    "root_inode",
                }
                or value["schema"] != _SCHEMA
            ):
                raise ValueError
            receipt = cls(
                value["retention_id"],
                value["phase"],
                value["binding_digest"],
                value["cleanup_receipt_digest"],
                value["transaction_digest"],
                value["lower_lease_set_id"],
                OCIRootVolumeRecord.from_dict(dict(value["source_volume"])),
                value["filesystem_uuid"],
                value["root_device"],
                value["root_inode"],
            )
            if (
                type(value["retained_generation"]) is not int
                or value["retained_generation"] != receipt.retained_generation
            ):
                raise ValueError
            return receipt
        except Exception:
            raise MonitorRootRetentionError("invalid root retention receipt") from None


def _resources(state: Mapping[str, Any], binding: MonitorPreActivationBinding, store: OCIStore):
    transaction = OCIRootPreparationTransaction.from_dict(state.get("oci_root"))
    value = state.get("oci_root_domain")
    if not isinstance(value, Mapping) or set(value) != {"digest", "plan"}:
        raise MonitorRootRetentionError("root retention domain plan is missing")
    plan = OCIRootDomainPlan.from_dict(value["plan"])
    if (
        transaction.phase != "resources-ready"
        or transaction.retention_policy != "retain"
        or value["digest"] != plan.digest
        or plan.digest != binding.plan_digest
        or plan.run_id != binding.record.run_id
        or plan.run_name != binding.record.name
        or plan.stage1_transport["artifact_digest"] != binding.stage1_artifact_digest
        or transaction.boot_plan_digest != plan.resource_plan_digest
        or transaction.lower_lease_set_id != plan.lower_lease_set_id
        or transaction.lower_graph_digest != plan.lower_graph_digest
        or transaction.volume_id != plan.root_volume["volume_id"]
        or transaction.volume_size_bytes != plan.root_volume["size_bytes"]
        or transaction.owner.run_id != binding.record.run_id
        or transaction.owner.run_name != binding.record.name
    ):
        raise MonitorRootRetentionError("root retention resource binding is invalid")
    leases = store.load_lease_set(
        transaction.lower_lease_set_id, transaction.owner, plan_digest=transaction.boot_plan_digest
    )
    if tuple(member.receipt for member in leases.members) != transaction.receipts:
        raise MonitorRootRetentionError("root retention original lower leases changed")
    expected = OCIRootVolumeRecord(
        transaction.volume_id,
        transaction.volume_size_bytes,
        transaction.lower_graph_digest,
        "retain",
        "attached",
        binding.record.run_id,
        binding.record.name,
        plan.root_volume["generation"],
    )
    return transaction, plan, expected, leases


def retain_inactive_monitor_root(
    roots: StatePaths,
    binding: MonitorPreActivationBinding,
    store: OCIStore,
    *,
    conn: Any,
    runner: CommandRunner = _default_runner,
    liveness_probe: Callable[[MonitorProcessIdentity], ProcessLiveness] = probe_process_liveness,
) -> MonitorRootRetentionReceipt:
    """Detach an originally-retain root only after exact completed cleanup.

    Keep the original lower leases, root bytes, journal and run evidence. A
    completed replay returns historical evidence even after a new VM claims
    the root; it does not inspect or change that newer attachment.
    """
    authority = None
    try:
        if (
            type(roots) is not StatePaths
            or type(binding) is not MonitorPreActivationBinding
            or type(store) is not OCIStore
            or not callable(liveness_probe)
            or not callable(runner)
            or store._root != roots.oci_derived_store.resolve()
        ):
            raise MonitorRootRetentionError("invalid root retention authority")
        binding.__post_init__()
        if binding.owner_uid != os.geteuid():
            raise MonitorRootRetentionError("root retention owner changed")
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            authority = _RecoveryAuthority(mutation, binding)
            journal = authority.snapshot
            cleanup = MonitorInactiveCleanupReceipt.from_dict(
                mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
            )
            expected_cleanup = MonitorInactiveCleanupReceipt(
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
            if cleanup != expected_cleanup:
                raise MonitorRootRetentionError("root retention completed cleanup evidence changed")
            transaction, plan, source, leases = _resources(mutation.snapshot.state, binding, store)

            def verify() -> None:
                authority.validate()
                _validate_ledger(mutation, binding, journal)
                if (
                    MonitorInactiveCleanupReceipt.from_dict(mutation.snapshot.state.get("oci_monitor_inactive_cleanup"))
                    != cleanup
                ):
                    raise MonitorRootRetentionError("root retention cleanup evidence changed")
                if _resources(mutation.snapshot.state, binding, store) != (transaction, plan, source, leases):
                    raise MonitorRootRetentionError("root retention resources changed")
                _stale(journal, liveness_probe)
                if _inspect_domain(conn, binding) is not None:
                    raise MonitorRootRetentionError("root retention requires both domain identifiers absent")
                authority.validate()
                _stale(journal, liveness_probe)
                if conn.getURI() != binding.libvirt_uri:
                    raise MonitorRootRetentionError("root retention connection URI changed")

            verify()
            existing = _STATE_KEY in mutation.snapshot.state
            receipt = MonitorRootRetentionReceipt.from_dict(mutation.snapshot.state[_STATE_KEY]) if existing else None

            def expected_receipt(identity: tuple[int, int]) -> MonitorRootRetentionReceipt:
                return MonitorRootRetentionReceipt(
                    receipt.retention_id if receipt is not None else str(uuid.uuid4()),
                    "intent",
                    binding.digest,
                    _digest(cleanup.to_dict()),
                    _digest(transaction.to_dict()),
                    transaction.lower_lease_set_id,
                    source,
                    plan.root_volume["filesystem_uuid"],
                    *identity,
                )

            if receipt is not None:
                if replace(receipt, phase="intent") != expected_receipt((receipt.root_device, receipt.root_inode)):
                    raise MonitorRootRetentionError("root retention intent binding changed")
                if receipt.phase == "completed":
                    verify()
                    return receipt

            def before_retention(current: OCIRootVolumeRecord, identity: tuple[int, int]) -> None:
                nonlocal receipt
                verify()
                if receipt is None:
                    if current != source:
                        raise MonitorRootRetentionError("retained root has no original detachment intent")
                    receipt = expected_receipt(identity)
                    data = mutation.mutable_state()
                    data[_STATE_KEY] = receipt.to_dict()
                    verify()
                    mutation.write_state(data["status"], data)
                elif replace(receipt, phase="intent") != expected_receipt(identity):
                    raise MonitorRootRetentionError("root retention data identity changed")
                verify()

            def after_retention(_retained: OCIRootVolumeRecord, identity: tuple[int, int]) -> None:
                nonlocal receipt
                verify()
                if receipt is None or replace(receipt, phase="intent") != expected_receipt(identity):
                    raise MonitorRootRetentionError("root retention completion binding changed")
                receipt = replace(receipt, phase="completed")
                data = mutation.mutable_state()
                data[_STATE_KEY] = receipt.to_dict()
                verify()
                mutation.write_state(data["status"], data)
                verify()

            _retain_exact_oci_root_volume(
                roots,
                source,
                filesystem_uuid=plan.root_volume["filesystem_uuid"],
                before_retention=before_retention,
                after_retention=after_retention,
                runner=runner,
            )
            assert receipt is not None and receipt.phase == "completed"
            return receipt
    except MonitorRootRetentionError:
        raise
    except Exception:
        raise MonitorRootRetentionError("inactive root retention could not be verified") from None
    finally:
        if authority is not None:
            authority.close()


__all__ = ["MonitorRootRetentionError", "MonitorRootRetentionReceipt", "retain_inactive_monitor_root"]
