"""Fail-closed public OCI-root proof projection tests."""

from __future__ import annotations

import json
import os
import stat
import uuid
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from palimpsest_local import oci_root_proof as proof_module
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import baseline_acl, grant_acl, traversal_acl
from palimpsest_local.oci_control_protocol_v2 import (
    OCI_CONTROL_CHANNEL_CARRIER,
    OCIControlV2Binding,
    OCIControlV2Message,
    encode_frame,
    sign_message,
    transcript_projection,
)
from palimpsest_local.oci_monitor_ipc import MonitorPreActivationBinding
from palimpsest_local.oci_root_proof import _PinnedRunRead, _ready_body, root_proof
from palimpsest_local.oci_runtime_access import RuntimeAccessReceipt, RuntimeAccessTarget
from palimpsest_local.oci_runtime_io import RuntimeIOReceipt
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind
from palimpsest_local.state import ExistingRunRecord, StatePaths

IDENTITY = {
    "schema": "palimpsest.oci-root-identity.v1",
    "pid": 1,
    "filesystem": "overlayfs",
    "device": 7,
    "inode": 11,
}


def _projection() -> dict[str, object]:
    return {
        "boot_attempt_id": "aca88126-d991-4de8-b66b-90dc07904dff",
        "boot_generation": "b22b1c81-dfa4-478a-b352-27b5b35fe5b7",
        "domain_core_digest": "sha256:" + "a" * 64,
        "epoch": 1,
        "host_nonce": "1" * 64,
        "reply_to": 2,
        "run_id": "f6f546e2-e734-4920-9eff-1762b348a249",
        "stage1_artifact_digest": "sha256:" + "b" * 64,
        "wire_sequence": 2,
    }


def test_ready_reconstruction_is_the_strict_protocol_body() -> None:
    body = _ready_body(_projection(), IDENTITY)
    message = OCIControlV2Message.from_dict(body)
    assert message.kind == "READY"
    assert dict(message.payload["root_identity"]) == IDENTITY
    assert json.loads(json.dumps(body, sort_keys=True)) == body


def test_missing_run_proof_is_read_only_and_does_not_create_lock_or_state(tmp_path: Path) -> None:
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    with pytest.raises(StateError):
        root_proof(roots, "missing")
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_pinned_reader_closes_ancestor_if_run_open_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "state" / "runs"
    runs.mkdir(parents=True)
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    snapshot = SimpleNamespace(record=SimpleNamespace(name="missing"))
    real_open = os.open
    opened: list[int] = []

    def tracked_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            raise FileNotFoundError
        descriptor = real_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises(FileNotFoundError):
        _PinnedRunRead(roots, snapshot)  # type: ignore[arg-type]
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_pinned_reader_rejects_byte_identical_visible_run_replacement(tmp_path: Path) -> None:
    runs = tmp_path / "state" / "runs"
    original = runs / "demo"
    original.mkdir(parents=True, mode=0o700)
    original.chmod(0o700)
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    snapshot = SimpleNamespace(record=SimpleNamespace(name="demo"))
    reader = _PinnedRunRead(roots, snapshot)  # type: ignore[arg-type]
    saved = runs / "saved"
    original.rename(saved)
    original.mkdir(mode=0o700)
    original.chmod(0o700)
    try:
        with pytest.raises(StateError, match="binding changed|ledger changed"):
            reader.verify_binding()
    finally:
        reader.close()


def test_pinned_reader_rejects_runs_ancestor_permission_drift(tmp_path: Path) -> None:
    runs = tmp_path / "state" / "runs"
    original = runs / "demo"
    original.mkdir(parents=True, mode=0o700)
    original.chmod(0o700)
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    snapshot = SimpleNamespace(record=SimpleNamespace(name="demo"))
    reader = _PinnedRunRead(roots, snapshot)  # type: ignore[arg-type]
    original_mode = stat.S_IMODE(runs.stat().st_mode)
    runs.chmod(original_mode ^ 0o020)
    try:
        with pytest.raises(StateError, match="binding changed|ledger changed"):
            reader.verify_binding()
    finally:
        runs.chmod(original_mode)
        reader.close()


def _runtime_access_receipt(run: Path) -> tuple[RuntimeAccessReceipt, MonitorPreActivationBinding]:
    record = ExistingRunRecord(
        "demo", "f6f546e2-e734-4920-9eff-1762b348a249", 2,
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )
    binding = MonitorPreActivationBinding(
        record, os.geteuid(), "sha256:" + "a" * 64, "sha256:" + "b" * 64,
        "sha256:" + "c" * 64, "de305d54-75b4-431b-adb2-eb6b9e546014",
        "aca88126-d991-4de8-b66b-90dc07904dff", "qemu:///system",
    )
    info = run.stat()
    qemu_uid = 12345 if os.geteuid() != 12345 else 12346
    qemu_gid = qemu_uid + 1
    directory_baseline = baseline_acl(directory=True)
    file_baseline = baseline_acl(directory=False)
    run_target = RuntimeAccessTarget(
        info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_nlink, True,
        directory_baseline, traversal_acl(directory_baseline, qemu_uid),
    )
    directory_target = RuntimeAccessTarget(
        info.st_dev, info.st_ino + 1, info.st_uid, info.st_gid, 2, True,
        directory_baseline, grant_acl(directory_baseline, qemu_uid),
    )
    console_target = RuntimeAccessTarget(
        info.st_dev, info.st_ino + 2, info.st_uid, info.st_gid, 1, False,
        file_baseline, grant_acl(file_baseline, qemu_uid),
    )
    runtime_io = RuntimeIOReceipt(
        "palimpsest.oci-runtime-io.v1", record.run_id, record.name, binding.plan_digest,
        directory_target.device, directory_target.inode, console_target.device, console_target.inode,
    )
    return RuntimeAccessReceipt(
        str(uuid.uuid4()), "granted", binding, runtime_io, qemu_uid, qemu_gid,
        run_target, directory_target, console_target,
    ), binding


@pytest.mark.parametrize(
    "mutation", ["valid", "missing", "malformed", "phase", "boot", "target", "acl"]
)
def test_pinned_reader_requires_exact_authorized_0710_receipt_and_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    runs = tmp_path / "state" / "runs"
    run = runs / "demo"
    run.mkdir(parents=True, mode=0o710)
    run.chmod(0o710)
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    receipt, binding = _runtime_access_receipt(run)
    serialized = receipt.to_dict()
    if mutation == "missing":
        state = {}
    else:
        state = {"oci_runtime_access": serialized}
        if mutation == "malformed":
            serialized.pop("schema")
        elif mutation == "phase":
            serialized["phase"] = "intent"
        elif mutation == "boot":
            serialized["binding"]["boot_attempt_id"] = str(uuid.uuid4())
        elif mutation == "target":
            serialized["run"]["inode"] += 3
            assert RuntimeAccessReceipt.from_dict(serialized).run.inode == receipt.run.inode + 3
    snapshot = SimpleNamespace(record=binding.record, state=_freeze(state))
    current_acl = receipt.run.baseline if mutation == "acl" else receipt.run.granted
    monkeypatch.setattr(
        proof_module, "LinuxFdACLBackend",
        lambda: SimpleNamespace(read_acl=lambda fd: current_acl),
    )
    monkeypatch.setattr(proof_module, "read_run_ledger_snapshot", lambda roots, name: snapshot)
    reader = _PinnedRunRead(roots, snapshot)  # type: ignore[arg-type]
    try:
        if mutation == "valid":
            reader.verify_authorized_mode(binding)
        else:
            with pytest.raises(StateError, match="binding is invalid"):
                reader.verify_authorized_mode(binding)
    finally:
        reader.close()


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _tree_fingerprint(root: Path) -> tuple[tuple[object, ...], ...]:
    values = []
    for path in sorted((root, *root.rglob("*"))):
        info = path.lstat()
        values.append(
            (
                str(path.relative_to(root.parent)), info.st_dev, info.st_ino,
                stat.S_IMODE(info.st_mode), info.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        )
    return tuple(values)


@pytest.fixture
def proof_case(monkeypatch: pytest.MonkeyPatch):
    run_id = "f6f546e2-e734-4920-9eff-1762b348a249"
    attempt = "aca88126-d991-4de8-b66b-90dc07904dff"
    generation = "b22b1c81-dfa4-478a-b352-27b5b35fe5b7"
    core = "sha256:" + "a" * 64
    stage1 = "sha256:" + "b" * 64
    plan_digest = "sha256:" + "c" * 64
    key = bytes(range(32))
    wire_binding = OCIControlV2Binding(run_id, core, stage1)
    ready_message = OCIControlV2Message(
        "READY", wire_binding, attempt, "1" * 64, 1, 2, {"root_identity": IDENTITY},
        boot_generation=generation, reply_to=2,
    )
    ready_envelope = sign_message(ready_message, key)
    projection = dict(
        transcript_projection(
            ready_envelope, encode_frame(ready_envelope), carrier=OCI_CONTROL_CHANNEL_CARRIER, key=key
        )
    )
    record = SimpleNamespace(name="demo", run_id=run_id)
    binding = SimpleNamespace(
        record=record, boot_attempt_id=attempt, domain_uuid=str(uuid.uuid4()), libvirt_uri="qemu:///system",
        plan_digest=plan_digest, stage1_artifact_digest=stage1,
        expected_definition_projection_digest="sha256:" + "d" * 64, digest="sha256:" + "e" * 64,
    )
    active_binding = SimpleNamespace(domain_id=7, boot_attempt_id=attempt)
    journal = SimpleNamespace(
        phase="ready", identity=SimpleNamespace(binding=binding, generation=str(uuid.uuid4())),
        active_binding=active_binding, revision=9,
    )
    lifecycle = {
        "phase": "ready", "boot_attempt_id": attempt, "boot_generation": generation,
        "key_id": ready_envelope.key_id, "terminal": None, "transcript": [projection],
    }
    handoff = {
        "schema": "palimpsest.oci-root-handoff.v1", "boot_attempt_id": attempt,
        "domain_uuid": binding.domain_uuid, "domain_id": 7, "plan_digest": plan_digest,
        "libvirt_uri": "qemu:///system", "phase": "ready", "lifecycle": lifecycle,
    }
    raw_state = {
        "status": "running", "oci_root_domain": {"digest": plan_digest, "plan": {}},
        "oci_root_handoff": handoff,
    }
    snapshots = [SimpleNamespace(record=record, state=_freeze(raw_state))]
    plan = SimpleNamespace(digest=plan_digest, domain_core_digest=core, stage1_transport={"artifact_digest": stage1})
    journals = [journal]
    domain_calls = []

    class Pinned:
        def __init__(self, roots, snapshot):
            self.snapshot = snapshot
            self.record = snapshot.record
            self._run_fd = 10

        def verify_binding(self):
            return None

        def verify_authorized_mode(self, binding):
            return None

        def close(self):
            self._run_fd = -1

    monkeypatch.setattr(proof_module, "_PinnedRunRead", Pinned)
    monkeypatch.setattr(proof_module, "load_oci_root_domain_plan", lambda roots, name: plan)
    monkeypatch.setattr(
        proof_module, "read_run_ledger_snapshot", lambda roots, name: snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
    )
    monkeypatch.setattr(
        proof_module, "_read_run_journal", lambda mutation, binding=None: journals.pop(0) if len(journals) > 1 else journals[0]
    )
    monkeypatch.setattr(proof_module, "_active_domain", lambda conn, selected, domain_id: domain_calls.append(domain_id))
    return SimpleNamespace(
        roots=SimpleNamespace(), snapshot=snapshots[0], raw_state=raw_state, handoff=handoff,
        lifecycle=lifecycle, projection=projection, binding=binding, journal=journal, plan=plan,
        snapshots=snapshots, journals=journals, domain_calls=domain_calls,
    )


def test_root_proof_accepts_immutable_exact_current_evidence_without_writes(proof_case) -> None:
    before = deepcopy(proof_case.raw_state)
    report = root_proof(proof_case.roots, "demo", conn=object())
    assert report["root_identity"] == IDENTITY
    assert report["run"] == {"name": "demo", "run_id": proof_case.binding.record.run_id}
    assert report["domain"]["id"] == 7
    assert proof_case.domain_calls == [7, 7]
    assert proof_case.raw_state == before


@pytest.mark.parametrize("valid", [True, False])
def test_root_proof_real_pinned_populated_tree_is_unchanged(
    proof_case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    runs = tmp_path / "state" / "runs"
    run = runs / "demo"
    private = run / "monitor-private"
    private.mkdir(parents=True, mode=0o700)
    run.chmod(0o700)
    (run / "ledger.json").write_text("persisted-ledger\n", encoding="ascii")
    (private / "journal.json").write_text("persisted-journal\n", encoding="ascii")
    proof_case.roots = StatePaths(tmp_path / "config", tmp_path / "state")
    monkeypatch.setattr(proof_module, "_PinnedRunRead", _PinnedRunRead)
    if not valid:
        proof_case.projection["body_digest"] = "sha256:" + "f" * 64
        proof_case.snapshot.state = _freeze(proof_case.raw_state)
    before = _tree_fingerprint(runs)
    if valid:
        root_proof(proof_case.roots, "demo", conn=object())
    else:
        with pytest.raises(StateError):
            root_proof(proof_case.roots, "demo", conn=object())
    assert _tree_fingerprint(runs) == before


@pytest.mark.parametrize("mutation", ["legacy", "duplicate", "body", "key", "boot", "plan-stage1"])
def test_root_proof_rejects_missing_conflicting_tampered_or_stale_ready(proof_case, mutation: str) -> None:
    if mutation == "legacy":
        for field in ("root_identity", "run_id", "domain_core_digest", "stage1_artifact_digest"):
            proof_case.projection.pop(field)
    elif mutation == "duplicate":
        proof_case.lifecycle["transcript"].append(deepcopy(proof_case.projection))
    elif mutation == "body":
        proof_case.projection["body_digest"] = "sha256:" + "f" * 64
    elif mutation == "key":
        proof_case.lifecycle["key_id"] = "sha256:" + "f" * 64
    elif mutation == "boot":
        proof_case.handoff["boot_attempt_id"] = str(uuid.uuid4())
    else:
        changed = "sha256:" + "f" * 64
        proof_case.projection["stage1_artifact_digest"] = changed
        proof_case.binding.stage1_artifact_digest = changed
        proof_case.projection["body_digest"] = proof_module._digest(
            _ready_body(proof_case.projection, IDENTITY)
        )
        proof_case.projection["projection_digest"] = proof_module._digest(
            {key: value for key, value in proof_case.projection.items() if key != "projection_digest"}
        )
    proof_case.snapshot.state = _freeze(proof_case.raw_state)
    before = deepcopy(proof_case.raw_state)
    with pytest.raises(StateError):
        root_proof(proof_case.roots, "demo", conn=object())
    assert proof_case.raw_state == before


def test_root_proof_rejects_journal_ledger_and_domain_observation_races(
    proof_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof_case.journals[:] = [proof_case.journal, SimpleNamespace(**{**vars(proof_case.journal), "revision": 10})]
    with pytest.raises(StateError, match="changed"):
        root_proof(proof_case.roots, "demo", conn=object())
    proof_case.journals[:] = [proof_case.journal]
    monkeypatch.setattr(
        proof_module, "_active_domain",
        lambda conn, binding, domain_id: (_ for _ in ()).throw(StateError("domain replaced")),
    )
    with pytest.raises(StateError, match="domain replaced"):
        root_proof(proof_case.roots, "demo", conn=object())


def test_root_proof_rejects_ledger_snapshot_race(proof_case) -> None:
    changed = deepcopy(proof_case.raw_state)
    changed["status"] = "exited"
    proof_case.snapshots[:] = [proof_case.snapshot, SimpleNamespace(record=proof_case.binding.record, state=_freeze(changed))]
    with pytest.raises(StateError, match="plan changed"):
        root_proof(proof_case.roots, "demo", conn=object())
