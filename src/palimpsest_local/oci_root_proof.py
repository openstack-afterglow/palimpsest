"""Read-only, path-free proof of one exact running OCI-root boot."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from collections.abc import Mapping
from typing import Any

from . import kvm
from .errors import StateError
from .oci_control_protocol_v2 import OCI_CONTROL_PROTOCOL_V2, OCIControlV2Message, validate_root_identity
from .oci_provenance import canonical_json_bytes
from .oci_root_kvm import load_oci_root_domain_plan
from .oci_root_runtime import _domain_projection, _projection_digest, connect_oci_root_libvirt
from .oci_run_cleanup import _read_run_journal
from .state import RunLedgerSnapshot, StatePaths, read_run_ledger_snapshot

OCI_ROOT_PROOF_SCHEMA = "palimpsest.oci-root-proof.v1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(value: Any) -> str:
    def plain(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: plain(child) for key, child in item.items()}
        if isinstance(item, tuple):
            return [plain(child) for child in item]
        return item

    return "sha256:" + hashlib.sha256(canonical_json_bytes(plain(value))).hexdigest()


def _ready_body(projection: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "boot_attempt_id": projection["boot_attempt_id"],
        "boot_generation": projection["boot_generation"],
        "domain_core_digest": projection["domain_core_digest"],
        "epoch": projection["epoch"],
        "host_nonce": projection["host_nonce"],
        "kind": "READY",
        "payload": {"root_identity": dict(identity)},
        "reply_to": projection["reply_to"],
        "run_id": projection["run_id"],
        "schema": OCI_CONTROL_PROTOCOL_V2,
        "stage1_artifact_digest": projection["stage1_artifact_digest"],
        "wire_sequence": projection["wire_sequence"],
    }


def _active_domain(conn: Any, binding: Any, domain_id: int) -> None:
    if conn.getURI() != binding.libvirt_uri:
        raise StateError("OCI-root proof connection binding changed")
    try:
        by_name = conn.lookupByName(binding.record.name)
        by_uuid = conn.lookupByUUIDString(binding.domain_uuid)
        inactive = kvm._libvirt().VIR_DOMAIN_XML_INACTIVE
        if by_name is None or by_uuid is None or type(inactive) is not int:
            raise ValueError
        for domain in (by_name, by_uuid):
            xml = domain.XMLDesc(inactive)
            if (
                domain.name() != binding.record.name
                or domain.UUIDString() != binding.domain_uuid
                or domain.isPersistent() != 1
                or domain.isActive() != 1
                or domain.ID() != domain_id
                or _projection_digest(_domain_projection(xml)) != binding.expected_definition_projection_digest
            ):
                raise ValueError
    except Exception:
        raise StateError("OCI-root proof active domain binding is invalid") from None


class _PinnedRunRead:
    def __init__(self, roots: StatePaths, snapshot: RunLedgerSnapshot):
        self.roots = roots
        self.snapshot = snapshot
        self.record = snapshot.record
        self._runs_fd = -1
        self._run_fd = -1
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            self._runs_fd = os.open(roots.runs, flags)
            self._run_fd = os.open(snapshot.record.name, flags, dir_fd=self._runs_fd)
            runs_open = os.fstat(self._runs_fd)
            runs_visible = os.stat(roots.runs, follow_symlinks=False)
            info = os.fstat(self._run_fd)
            visible = os.stat(snapshot.record.name, dir_fd=self._runs_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(runs_open.st_mode)
                or not stat.S_ISDIR(runs_visible.st_mode)
                or (runs_open.st_dev, runs_open.st_ino) != (runs_visible.st_dev, runs_visible.st_ino)
                or not stat.S_ISDIR(info.st_mode)
                or not stat.S_ISDIR(visible.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != (visible.st_dev, visible.st_ino)
            ):
                raise StateError("OCI-root proof run binding is invalid")
            self._runs_identity = (runs_open.st_dev, runs_open.st_ino)
            self._runs_metadata = (runs_open.st_uid, stat.S_IMODE(runs_open.st_mode))
            self._run_identity = (info.st_dev, info.st_ino)
        except Exception:
            self.close()
            raise

    def verify_binding(self) -> None:
        try:
            runs_open = os.fstat(self._runs_fd)
            runs_visible = os.stat(self.roots.runs, follow_symlinks=False)
            run_open = os.fstat(self._run_fd)
            run_visible = os.stat(self.record.name, dir_fd=self._runs_fd, follow_symlinks=False)
        except OSError:
            raise StateError("OCI-root proof run binding changed") from None
        if (
            any(not stat.S_ISDIR(item.st_mode) for item in (runs_open, runs_visible, run_open, run_visible))
            or any(
                (item.st_uid, stat.S_IMODE(item.st_mode)) != self._runs_metadata
                for item in (runs_open, runs_visible)
            )
            or any(item.st_uid != os.geteuid() for item in (run_open, run_visible))
            or any(stat.S_IMODE(item.st_mode) != 0o700 for item in (run_open, run_visible))
            or any((item.st_dev, item.st_ino) != self._runs_identity for item in (runs_open, runs_visible))
            or any((item.st_dev, item.st_ino) != self._run_identity for item in (run_open, run_visible))
            or read_run_ledger_snapshot(self.roots, self.record.name) != self.snapshot
        ):
            raise StateError("OCI-root proof run ledger changed")

    def close(self) -> None:
        if self._run_fd >= 0:
            os.close(self._run_fd)
            self._run_fd = -1
        if self._runs_fd >= 0:
            os.close(self._runs_fd)
            self._runs_fd = -1


def root_proof(roots: StatePaths, name: str, *, conn: Any | None = None) -> Mapping[str, Any]:
    """Return authenticated-at-receipt evidence for the current running boot.

    This is durable monitor evidence, not offline cryptographic or TPM
    attestation.  The boot key and MAC are intentionally never returned.
    """

    own_connection = conn is None
    opened = None
    try:
        snapshot = read_run_ledger_snapshot(roots, name)
        plan = load_oci_root_domain_plan(roots, name)
        if read_run_ledger_snapshot(roots, name) != snapshot or plan.digest != snapshot.state["oci_root_domain"]["digest"]:
            raise StateError("OCI-root proof domain plan changed")
        mutation = _PinnedRunRead(roots, snapshot)
        try:
            journal = _read_run_journal(mutation)
            binding = journal.identity.binding
            handoff = snapshot.state.get("oci_root_handoff")
            if (
                journal.phase != "ready"
                or not isinstance(handoff, Mapping)
                or snapshot.state.get("status") != "running"
                or handoff.get("phase") != "ready"
                or type(handoff.get("domain_id")) is not int
                or binding.record != snapshot.record
                or plan.digest != binding.plan_digest
                or journal.active_binding is None
                or journal.active_binding.domain_id != handoff.get("domain_id")
                or journal.active_binding.boot_attempt_id != binding.boot_attempt_id
                or handoff.get("boot_attempt_id") != binding.boot_attempt_id
                or handoff.get("domain_uuid") != binding.domain_uuid
                or handoff.get("plan_digest") != binding.plan_digest
                or handoff.get("libvirt_uri") != binding.libvirt_uri
            ):
                raise StateError("OCI-root proof requires the current running READY boot")
            lifecycle = handoff.get("lifecycle")
            if (
                not isinstance(lifecycle, Mapping)
                or lifecycle.get("phase") != "ready"
                or lifecycle.get("boot_attempt_id") != binding.boot_attempt_id
                or lifecycle.get("terminal") is not None
            ):
                raise StateError("OCI-root proof READY receipt is missing")
            transcript = lifecycle.get("transcript")
            candidates = [item for item in transcript if isinstance(item, Mapping) and item.get("kind") == "READY"] \
                if isinstance(transcript, tuple) else []
            if len(candidates) != 1:
                raise StateError("OCI-root proof has missing or conflicting READY evidence")
            ready = candidates[0]
            required = {
                "authentication_verified", "body_digest", "boot_attempt_id", "boot_generation", "carrier",
                "direction", "domain_core_digest", "envelope_digest", "epoch", "host_nonce", "key_id", "kind",
                "projection_digest", "reply_to", "request_id", "root_identity", "run_id", "size_bytes",
                "stage1_artifact_digest", "wire_sequence",
            }
            if (
                set(ready) != required
                or ready["authentication_verified"] is not True
                or any(
                    not isinstance(ready.get(field), str) or _DIGEST_RE.fullmatch(ready[field]) is None
                    for field in ("body_digest", "envelope_digest", "projection_digest", "key_id")
                )
                or type(ready.get("size_bytes")) is not int
                or not 5 <= ready["size_bytes"] <= 64 * 1024
            ):
                raise StateError("OCI-root proof READY projection is invalid")
            projection_subject = {key: value for key, value in ready.items() if key != "projection_digest"}
            if not hmac.compare_digest(ready["projection_digest"], _digest(projection_subject)):
                raise StateError("OCI-root proof READY projection digest is invalid")
            identity = validate_root_identity(ready["root_identity"])
            body = _ready_body(ready, identity)
            message = OCIControlV2Message.from_dict(body)
            if (
                message.kind != "READY"
                or message.payload["root_identity"] != identity
                or ready["boot_attempt_id"] != binding.boot_attempt_id
                or ready["boot_generation"] != lifecycle.get("boot_generation")
                or ready["key_id"] != lifecycle.get("key_id")
                or ready["run_id"] != binding.record.run_id
                or ready["domain_core_digest"] != plan.domain_core_digest
                or ready["stage1_artifact_digest"] != binding.stage1_artifact_digest
                or ready["stage1_artifact_digest"] != plan.stage1_transport["artifact_digest"]
                or ready["direction"] != "guest-to-host"
                or ready["carrier"] != "channel-frame"
                or ready["request_id"] is not None
                or not hmac.compare_digest(ready["body_digest"], _digest(body))
            ):
                raise StateError("OCI-root proof READY evidence binding is invalid")
            opened = connect_oci_root_libvirt(binding.libvirt_uri) if own_connection else conn
            _active_domain(opened, binding, handoff["domain_id"])
            if _read_run_journal(mutation, binding) != journal or read_run_ledger_snapshot(roots, name) != snapshot:
                raise StateError("OCI-root proof evidence changed during validation")
            _active_domain(opened, binding, handoff["domain_id"])
            report = {
                "binding_digest": binding.digest,
                "boot": {"attempt_id": binding.boot_attempt_id, "generation": lifecycle["boot_generation"]},
                "domain": {"id": handoff["domain_id"], "uuid": binding.domain_uuid},
                "journal": {"generation": journal.identity.generation, "revision": journal.revision},
                "ledger_digest": _digest(handoff),
                "ready": {"body_digest": ready["body_digest"], "envelope_digest": ready["envelope_digest"]},
                "root_identity": identity,
                "run": {"name": binding.record.name, "run_id": binding.record.run_id},
                "schema": OCI_ROOT_PROOF_SCHEMA,
            }
            return report
        finally:
            mutation.close()
    except StateError:
        raise
    except Exception:
        raise StateError("OCI-root proof validation failed") from None
    finally:
        if own_connection and opened is not None:
            try:
                opened.close()
            except Exception:
                pass


__all__ = ["OCI_ROOT_PROOF_SCHEMA", "root_proof"]
