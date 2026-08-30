"""Fail-closed existing-run dispatch from durable runtime ledgers."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat as stat_module
import subprocess
import sys
import tempfile
import traceback
import uuid
from collections import UserDict
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from palimpsest_local import runtime_dispatch, state
from palimpsest_local.errors import StateError
from palimpsest_local.refs import ImageRef, RunSpec, StackRef, VolumeAttachment
from palimpsest_local.runtime_types import (
    ALLOWED_RUNTIME_COMBINATIONS,
    CapabilityCheck,
    CapabilityErrorCategory,
    CloudInitSnapshot,
    CommitResult,
    DispatchKey,
    ExecRequest,
    ExistingRunRecord,
    ExpectedRunIdentity,
    InspectRecord,
    LifecycleCursor,
    LifecycleResult,
    LifecycleWarningCategory,
    LogErrorCategory,
    LogTerminalCategory,
    LogTerminalEvent,
    PreflightReport,
    ProcessCapabilities,
    ProcessExit,
    ProcessExitCategory,
    ProcessSignal,
    ResolvedRunRequest,
    RunAggregationError,
    RunAggregationResult,
    RunAttachmentMode,
    RunResult,
    RunSummary,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RuntimeOperation,
    RuntimePreflightError,
    RunVolumeIntent,
    RunWarningCategory,
    _issue_lifecycle_adapter_outcome,
    _LifecycleAdapterOutcome,
    existing_record_subject_digest,
    run_request_subject_digest,
)


def _roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )


def _create_spec(
    tmp_path: Path,
    *,
    arch: str = "x86_64",
    name: str = "create-demo",
    volumes: tuple[VolumeAttachment, ...] = (),
) -> RunSpec:
    content = f"cloud-{arch}".encode()
    image = tmp_path / f"{name}-{arch}.qcow2"
    image.write_bytes(content)
    base = ImageRef(
        "sha256:" + hashlib.sha256(content).hexdigest(),
        "qcow2",
        arch,  # type: ignore[arg-type]
        None,
        image,
    )
    return RunSpec(
        name=name,
        stack=StackRef(base, ()),
        environment=(("PRIVATE_VALUE", "do-not-reflect"),),
        volumes=volumes,
    )


@pytest.fixture(autouse=True)
def _stub_operation_capability_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "test-present", True),
    )


def _authorized_request(request: ResolvedRunRequest) -> ResolvedRunRequest:
    stage = (
        runtime_dispatch.RunRequestProvenanceStage.BOUND
        if request.attachments_bound
        else runtime_dispatch.RunRequestProvenanceStage.LOGICAL
    )
    return runtime_dispatch._issue_run_request_provenance(request, stage)


def _bind_authorized(
    request: ResolvedRunRequest,
    final_spec: RunSpec,
    *,
    dispatch_key: DispatchKey,
) -> ResolvedRunRequest:
    receipt = runtime_dispatch._issue_volume_binding_receipt(
        request,
        final_spec,
        dispatch_key=dispatch_key,
        _authority=runtime_dispatch._PROJECT_VOLUME_BINDING_AUTHORITY,
    )
    return runtime_dispatch.bind_run_request_volumes(
        request,
        final_spec,
        dispatch_key=dispatch_key,
        receipt=receipt,
    )


def _successful_run_preflight(request: ResolvedRunRequest) -> PreflightReport:
    return runtime_dispatch.preflight_run_request(request)


def _write_ledger(
    roots: state.StatePaths,
    *,
    name: str = "demo",
    owner: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
    complete_v2_identity: bool = True,
) -> tuple[state.RunPaths, str]:
    rpaths = state.run_paths(roots, name)
    rpaths.root.mkdir(parents=True)
    run_id = str(uuid.uuid4())
    owner_payload = owner if owner is not None else {"schema_version": 1, "run_id": run_id, "name": name}
    state_payload = (
        dict(record)
        if record is not None
        else {"name": name, "run_id": owner_payload.get("run_id", run_id), "status": "stopped"}
    )
    if complete_v2_identity and state_payload.get("schema_version") == 2:
        state_payload.setdefault("name", name)
        state_payload.setdefault("run_id", owner_payload.get("run_id", run_id))
    rpaths.owner.write_text(json.dumps(owner_payload, sort_keys=True) + "\n", encoding="utf-8")
    rpaths.state.write_text(json.dumps(state_payload, sort_keys=True) + "\n", encoding="utf-8")
    rpaths.owner.chmod(0o600)
    rpaths.state.chmod(0o600)
    return rpaths, run_id


def _snapshot_tree(root: Path) -> dict[str, tuple[str, int, int, bytes | str | None]]:
    result: dict[str, tuple[str, int, int, bytes | str | None]] = {}
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat_module.S_IMODE(metadata.st_mode)
        if stat_module.S_ISREG(metadata.st_mode):
            result[relative] = ("file", mode, metadata.st_mtime_ns, path.read_bytes())
        elif stat_module.S_ISDIR(metadata.st_mode):
            result[relative] = ("directory", mode, metadata.st_mtime_ns, None)
        elif stat_module.S_ISLNK(metadata.st_mode):
            result[relative] = ("symlink", mode, metadata.st_mtime_ns, os.readlink(path))
        else:
            result[relative] = ("other", mode, metadata.st_mtime_ns, None)
    return result


def _replace_ledger_with_oci(roots: state.StatePaths, rpaths: state.RunPaths) -> None:
    replacement_id = str(uuid.uuid4())
    rpaths.owner.write_text(
        json.dumps({"schema_version": 1, "run_id": replacement_id, "name": "demo"}) + "\n",
        encoding="utf-8",
    )
    rpaths.state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_kind": "oci-root",
                "backend": "kvm",
                "name": "demo",
                "run_id": replacement_id,
                "status": "stopped",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _run_dispatch_reader_subprocess(
    roots: state.StatePaths, *, setup_source: str = ""
) -> subprocess.CompletedProcess[str]:
    source = f"""
import os
import sys
import time
from pathlib import Path

from palimpsest_local import state
from palimpsest_local.errors import StateError

roots = state.StatePaths(Path(sys.argv[1]), Path(sys.argv[2]))
{setup_source}
fd_directory = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
before = len(os.listdir(fd_directory))
started = time.monotonic()
for _ in range(32):
    try:
        state.read_run_dispatch_record(roots, "demo")
    except StateError as exc:
        assert str(exc) == "cannot securely read run ledger"
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        raise AssertionError("non-regular ledger was accepted")
after = len(os.listdir(fd_directory))
assert time.monotonic() - started < 1.0
assert after == before, (before, after)
print("ok")
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    return subprocess.run(
        [sys.executable, "-c", source, str(roots.config), str(roots.state)],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
        env=environment,
    )


def test_runtime_type_contract_has_only_the_phase_one_dispatch_combinations() -> None:
    assert ALLOWED_RUNTIME_COMBINATIONS == frozenset(
        {
            (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
            (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
            (RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
        }
    )
    with pytest.raises(ValueError, match="unsupported runtime/backend combination"):
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.LIMA_VZ)
    with pytest.raises(TypeError, match="RuntimeKind and RuntimeBackend"):
        DispatchKey("cloud-image", "kvm")  # type: ignore[arg-type]


def test_create_resolver_is_pure_and_request_does_not_reflect_its_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _create_spec(tmp_path)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "select_backend",
        lambda arch, requested="auto": calls.append((arch, requested)) or "kvm",
    )
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "preflight",
        lambda *_args, **_kwargs: pytest.fail("resolver entered backend preflight"),
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "run",
        lambda *_args, **_kwargs: pytest.fail("resolver entered cloud adapter"),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        "run",
        lambda *_args, **_kwargs: pytest.fail("resolver entered Lima adapter"),
    )

    request = runtime_dispatch.resolve_run_request(spec, requested_backend="kvm")

    assert request.dispatch_key == DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    assert request.spec is spec
    assert request.attachments_bound is True
    assert calls == [("x86_64", "kvm")]
    rendered = repr(request)
    assert "do-not-reflect" not in rendered
    assert str(spec.stack.base.local_path) not in rendered
    with pytest.raises(FrozenInstanceError):
        request.attachments_bound = False  # type: ignore[misc]


def test_run_resolver_rejects_hvf_network_before_preflight_or_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = replace(_create_spec(tmp_path, arch="aarch64"), network="routed")
    monkeypatch.setattr(runtime_dispatch.platforms, "select_backend", lambda *_args, **_kwargs: "libvirt-hvf")
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "capability_profile",
        lambda *_args, **_kwargs: pytest.fail("network validation reached preflight"),
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "run",
        lambda *_args, **_kwargs: pytest.fail("network validation reached adapter"),
    )

    with pytest.raises(StateError, match="only none or default"):
        runtime_dispatch.resolve_run_request(spec, requested_backend="libvirt-hvf")


@pytest.mark.parametrize("attack", ["kvm-host-path", "lima-backend-format"])
def test_public_run_resolver_rejects_direct_physical_volume_attachments(
    attack: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if attack == "kvm-host-path":
        spec = replace(
            _create_spec(tmp_path),
            volumes=(VolumeAttachment("data", "/srv/data", host_path=Path("/etc/hosts")),),
        )
    else:
        spec = replace(
            _create_spec(tmp_path, arch="aarch64"),
            volumes=(
                VolumeAttachment(
                    "data",
                    "/srv/data",
                    backend_name="arbitrary-existing-disk",
                    format=True,
                ),
            ),
        )
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "select_backend",
        lambda *_args, **_kwargs: pytest.fail("backend selection reached"),
    )

    with pytest.raises(StateError, match="private project volume binder"):
        runtime_dispatch.resolve_run_request(spec)


def test_create_preflight_returns_a_request_bound_capability_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            _create_spec(tmp_path),
        )
    )
    expected = _successful_run_preflight(request)
    calls: list[object] = []

    def evaluate(profile, **kwargs):
        calls.append((profile, kwargs))
        return expected

    monkeypatch.setattr(runtime_dispatch.platforms, "evaluate_capability_profile", evaluate)

    returned = runtime_dispatch.preflight_run_request(request)

    assert returned is expected
    assert returned.subject_digest == run_request_subject_digest(request)
    assert returned.profile.operation is RuntimeOperation.RUN
    assert len(calls) == 1


def test_run_preflight_binding_survives_only_physical_volume_binding(tmp_path: Path) -> None:
    intent = RunVolumeIntent("data", "/srv/data")
    logical = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            _create_spec(tmp_path),
            (intent,),
            attachments_bound=False,
        )
    )
    report = _successful_run_preflight(logical)
    volume_path = tmp_path / "data.raw"
    volume_path.write_bytes(b"volume")
    final_spec = replace(
        logical.spec,
        volumes=(VolumeAttachment("data", "/srv/data", host_path=volume_path),),
    )

    bound = _bind_authorized(
        logical,
        final_spec,
        dispatch_key=logical.dispatch_key,
    )

    assert run_request_subject_digest(bound) == report.subject_digest
    runtime_dispatch.require_run_preflight(bound, report, now_ns=report.issued_at_monotonic_ns)

    mismatch_report = _successful_run_preflight(logical)
    changed = _authorized_request(
        ResolvedRunRequest(
            logical.dispatch_key,
            replace(logical.spec, network="other-network"),
            logical.volume_intents,
            attachments_bound=False,
        )
    )
    with pytest.raises(RuntimePreflightError) as captured:
        runtime_dispatch.require_run_preflight(
            changed,
            mismatch_report,
            now_ns=mismatch_report.issued_at_monotonic_ns,
        )
    assert captured.value.category is CapabilityErrorCategory.REPORT_MISMATCH


def test_stale_or_missing_run_preflight_fails_before_state_or_adapter_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            _create_spec(tmp_path),
        )
    )
    current = _successful_run_preflight(request)
    stale = runtime_dispatch.preflight_run_request(request, now_ns=10)
    roots = state.StatePaths(tmp_path / "new-config", tmp_path / "new-state")
    effects: list[str] = []
    monkeypatch.setattr(
        runtime_dispatch.state,
        "init_resolved_roots",
        lambda _roots: effects.append("state") or pytest.fail("state initialization reached"),
    )
    monkeypatch.setattr(
        runtime_dispatch.state,
        "_retry_run_deletion_quarantines",
        lambda *_args, **_kwargs: effects.append("quarantine") or pytest.fail("quarantine cleanup reached"),
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime, "run", lambda *_a, **_k: effects.append("cloud") or pytest.fail()
    )
    monkeypatch.setattr(runtime_dispatch.lima, "run", lambda *_a, **_k: effects.append("lima") or pytest.fail())
    monkeypatch.setattr(runtime_dispatch.platforms.time, "monotonic_ns", lambda: stale.expires_at_monotonic_ns)

    with pytest.raises(RuntimePreflightError) as stale_error:
        runtime_dispatch.run(request, preflight=stale, roots=roots)
    assert stale_error.value.category is CapabilityErrorCategory.REPORT_STALE
    mismatched = _authorized_request(
        replace(request, spec=replace(request.spec, network="other-network"), provenance=None)
    )
    with pytest.raises(RuntimePreflightError) as mismatch_error:
        runtime_dispatch.run(mismatched, preflight=current, roots=roots)
    assert mismatch_error.value.category is CapabilityErrorCategory.REPORT_MISMATCH
    with pytest.raises(RuntimePreflightError) as missing_error:
        runtime_dispatch.run(request, preflight=None, roots=roots)
    assert missing_error.value.category is CapabilityErrorCategory.REPORT_PROVENANCE
    assert effects == []
    assert not roots.config.exists()
    assert not roots.state.exists()


@pytest.mark.parametrize(
    ("backend", "arch", "network_capability"),
    [
        (RuntimeBackend.KVM, "x86_64", "network.libvirt"),
        (RuntimeBackend.LIMA_VZ, "aarch64", "network.lima"),
    ],
)
def test_create_network_preflight_failure_has_zero_state_or_adapter_mutation(
    backend: RuntimeBackend,
    arch: str,
    network_capability: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_dispatch.platforms, "select_backend", lambda *_args, **_kwargs: backend.value)
    request = runtime_dispatch.resolve_run_request(
        replace(_create_spec(tmp_path, arch=arch), network="private-net"),
        requested_backend=backend.value,
    )
    roots = state.StatePaths(tmp_path / "new-config", tmp_path / "new-state")
    effects: list[str] = []

    def check(requirement, **_kwargs):
        if requirement.capability_id == network_capability:
            assert requirement.selector == "private-net"
            return CapabilityCheck(
                requirement.capability_id,
                "unavailable",
                False,
                CapabilityErrorCategory.CHECK_FAILED,
                "requested network unavailable",
            )
        return CapabilityCheck(requirement.capability_id, "present", True)

    monkeypatch.setattr(runtime_dispatch.platforms, "_check_capability", check)
    monkeypatch.setattr(
        runtime_dispatch.state,
        "init_resolved_roots",
        lambda *_args, **_kwargs: effects.append("state") or pytest.fail("state initialization reached"),
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "run",
        lambda *_args, **_kwargs: effects.append("cloud") or pytest.fail("cloud adapter reached"),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        "run",
        lambda *_args, **_kwargs: effects.append("lima") or pytest.fail("Lima adapter reached"),
    )

    with pytest.raises(RuntimePreflightError) as captured:
        runtime_dispatch.preflight_run_request(request)

    assert captured.value.category is CapabilityErrorCategory.CHECK_FAILED
    assert captured.value.capability_id == network_capability
    assert effects == []
    assert not roots.config.exists()
    assert not roots.state.exists()


def test_compose_early_remote_pull_gate_uses_host_tool_profile_until_network_is_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[str] = []
    monkeypatch.setattr(runtime_dispatch.platforms, "select_backend", lambda *_args, **_kwargs: "kvm")
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: (
            checked.append(requirement.capability_id) or CapabilityCheck(requirement.capability_id, "present", True)
        ),
    )

    key = runtime_dispatch.preflight_run_capabilities(
        "x86_64",
        requested_backend="kvm",
        network=None,
        host=runtime_dispatch.platforms.HostPlatform("Linux", "x86_64"),
    )

    assert key.backend is RuntimeBackend.KVM
    assert "network.libvirt" not in checked


def test_existing_record_subject_binding_changes_with_every_authority_field() -> None:
    record = ExistingRunRecord(
        "demo",
        "00000000-0000-0000-0000-000000000001",
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    baseline = existing_record_subject_digest(record)

    assert existing_record_subject_digest(record) == baseline
    assert existing_record_subject_digest(replace(record, state_schema_version=1)) != baseline
    assert existing_record_subject_digest(replace(record, run_id="00000000-0000-0000-0000-000000000002")) != baseline
    assert (
        existing_record_subject_digest(
            replace(record, dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ))
        )
        != baseline
    )


@pytest.mark.parametrize(
    "operation",
    [
        RuntimeOperation.START,
        RuntimeOperation.STOP,
        RuntimeOperation.RM,
        RuntimeOperation.INSPECT,
        RuntimeOperation.LOGS,
        RuntimeOperation.RECONCILE,
        RuntimeOperation.EXEC,
        RuntimeOperation.SHELL,
    ],
)
def test_existing_operation_report_is_bound_to_exact_operation_record_host_and_one_use(
    operation: RuntimeOperation,
) -> None:
    host = runtime_dispatch.platforms.HostPlatform("Linux", "x86_64")
    record = ExistingRunRecord(
        "demo",
        "00000000-0000-0000-0000-000000000001",
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    report = runtime_dispatch.preflight_existing_record(record, operation, host=host, now_ns=100)

    runtime_dispatch.require_existing_preflight(record, operation, report, host=host, now_ns=100)
    with pytest.raises(RuntimePreflightError) as reused:
        runtime_dispatch.require_existing_preflight(record, operation, report, host=host, now_ns=100)
    assert reused.value.category is CapabilityErrorCategory.REPORT_CONSUMED


@pytest.mark.parametrize("mismatch", ["operation", "record", "host", "stale", "forged"])
def test_existing_operation_report_mismatch_or_forgery_burns_token_without_adapter_side_effect(
    mismatch: str,
) -> None:
    host = runtime_dispatch.platforms.HostPlatform("Linux", "x86_64")
    record = ExistingRunRecord(
        "demo",
        "00000000-0000-0000-0000-000000000001",
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    report = runtime_dispatch.preflight_existing_record(record, RuntimeOperation.START, host=host, now_ns=100)
    expected_record = record
    expected_operation = RuntimeOperation.START
    expected_host = host
    expected_now = 100
    expected_category = CapabilityErrorCategory.REPORT_MISMATCH
    if mismatch == "operation":
        expected_operation = RuntimeOperation.STOP
    elif mismatch == "record":
        expected_record = replace(record, run_id="00000000-0000-0000-0000-000000000002")
    elif mismatch == "host":
        expected_host = runtime_dispatch.platforms.HostPlatform("Darwin", "aarch64")
    elif mismatch == "stale":
        expected_now = report.expires_at_monotonic_ns
        expected_category = CapabilityErrorCategory.REPORT_STALE
    else:
        report = replace(report, authentication_tag="0" * 64)
        expected_category = CapabilityErrorCategory.REPORT_PROVENANCE

    with pytest.raises(RuntimePreflightError) as captured:
        runtime_dispatch.require_existing_preflight(
            expected_record,
            expected_operation,
            report,
            host=expected_host,
            now_ns=expected_now,
        )
    assert captured.value.category is expected_category
    if mismatch != "forged":
        with pytest.raises(RuntimePreflightError) as burned:
            runtime_dispatch.require_existing_preflight(
                record,
                RuntimeOperation.START,
                report,
                host=host,
                now_ns=100,
            )
        assert burned.value.category is CapabilityErrorCategory.REPORT_CONSUMED


def test_existing_operation_capability_failure_precedes_adapter_and_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
        },
    )
    before = _snapshot_tree(roots.state)
    effects: list[str] = []
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(
            requirement.capability_id,
            "missing",
            False,
            CapabilityErrorCategory.MISSING,
            "test capability missing",
        ),
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "start",
        lambda *_args, **_kwargs: effects.append("adapter") or pytest.fail("adapter reached"),
    )

    with pytest.raises(RuntimePreflightError) as captured:
        runtime_dispatch.start("demo", roots=roots)

    assert captured.value.category is CapabilityErrorCategory.MISSING
    assert effects == []
    assert _snapshot_tree(roots.state) == before


def test_oci_create_resolution_fails_before_host_or_runtime_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _create_spec(tmp_path)
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "select_backend",
        lambda *_args, **_kwargs: pytest.fail("OCI resolver selected a host backend"),
    )
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "preflight",
        lambda *_args, **_kwargs: pytest.fail("OCI resolver entered backend preflight"),
    )
    monkeypatch.setattr(
        runtime_dispatch.state,
        "resolve_roots",
        lambda *_args, **_kwargs: pytest.fail("OCI resolver accessed runtime state"),
    )

    with pytest.raises(RuntimeCapabilityError) as exc_info:
        runtime_dispatch.resolve_run_request(
            spec,
            runtime_kind=RuntimeKind.OCI_ROOT,
            requested_backend="lima-vz",
        )

    assert exc_info.value.operation is RuntimeOperation.RUN
    assert exc_info.value.dispatch_key == DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)
    assert str(exc_info.value) == "runtime operation 'run' is unavailable for oci-root/kvm"


def test_volume_binding_changes_only_prepared_attachments(tmp_path: Path) -> None:
    logical_spec = _create_spec(tmp_path)
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    intent = RunVolumeIntent("data", "/srv/data", "ext4", False)
    request = _authorized_request(ResolvedRunRequest(key, logical_spec, (intent,), attachments_bound=False))
    volume_path = tmp_path / "data.raw"
    volume_path.write_bytes(b"volume")
    attachment = VolumeAttachment("data", "/srv/data", host_path=volume_path)
    final_spec = RunSpec(**{**logical_spec.__dict__, "volumes": (attachment,)})

    bound = _bind_authorized(request, final_spec, dispatch_key=key)

    assert bound.dispatch_key == key
    assert bound.attachments_bound is True
    assert bound.volume_intents == (intent,)
    assert bound.spec.volumes == (attachment,)
    with pytest.raises(StateError, match="immutable run inputs"):
        _bind_authorized(
            request,
            RunSpec(**{**final_spec.__dict__, "memory_mib": final_spec.memory_mib + 1}),
            dispatch_key=key,
        )
    with pytest.raises(StateError, match="dispatch identity"):
        _bind_authorized(
            request,
            final_spec,
            dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
        )


def test_volume_binding_rejects_missing_extra_reordered_or_substituted_intent(tmp_path: Path) -> None:
    logical_spec = _create_spec(tmp_path)
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    intents = (
        RunVolumeIntent("alpha", "/srv/alpha", "ext4", False),
        RunVolumeIntent("beta", "/srv/beta", "ext4", True),
    )
    request = _authorized_request(ResolvedRunRequest(key, logical_spec, intents, attachments_bound=False))
    alpha_path = tmp_path / "alpha.raw"
    beta_path = tmp_path / "beta.raw"
    gamma_path = tmp_path / "gamma.raw"
    for path in (alpha_path, beta_path, gamma_path):
        path.write_bytes(path.name.encode())
    alpha = VolumeAttachment("alpha", "/srv/alpha", host_path=alpha_path)
    beta = VolumeAttachment("beta", "/srv/beta", host_path=beta_path, read_only=True)
    gamma = VolumeAttachment("gamma", "/srv/gamma", host_path=gamma_path)

    invalid_attachments = (
        (alpha,),
        (alpha, beta, gamma),
        (beta, alpha),
        (alpha, VolumeAttachment("beta", "/srv/substitute", host_path=beta_path, read_only=True)),
        (alpha, VolumeAttachment("beta", "/srv/beta", host_path=beta_path, read_only=False)),
    )
    for attachments in invalid_attachments:
        with pytest.raises(StateError, match="logical volume intent"):
            _bind_authorized(
                request,
                RunSpec(**{**logical_spec.__dict__, "volumes": attachments}),
                dispatch_key=key,
            )

    assert "srv/alpha" not in repr(request)
    assert "alpha" not in repr(request)


def test_bound_request_constructor_rejects_attachment_source_for_other_backend(tmp_path: Path) -> None:
    intent = RunVolumeIntent("data", "/srv/data")
    lima_attachment = VolumeAttachment("data", "/srv/data", backend_name="data-disk")
    kvm_spec = _create_spec(tmp_path, volumes=(lima_attachment,))
    with pytest.raises(ValueError, match="source does not match resolved backend"):
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            kvm_spec,
            (intent,),
        )

    host_path = tmp_path / "data.raw"
    host_path.write_bytes(b"data")
    kvm_attachment = VolumeAttachment("data", "/srv/data", host_path=host_path)
    lima_spec = _create_spec(tmp_path, arch="aarch64", name="lima-source", volumes=(kvm_attachment,))
    with pytest.raises(ValueError, match="source does not match resolved backend"):
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
            lima_spec,
            (intent,),
        )


@pytest.mark.parametrize(
    ("backend", "arch", "attachment_kind"),
    [
        (RuntimeBackend.KVM, "x86_64", "backend-name"),
        (RuntimeBackend.LIBVIRT_HVF, "aarch64", "backend-name"),
        (RuntimeBackend.LIMA_VZ, "aarch64", "host-path"),
    ],
)
def test_volume_binder_rejects_attachment_source_for_other_backend(
    tmp_path: Path,
    backend: RuntimeBackend,
    arch: str,
    attachment_kind: str,
) -> None:
    logical = _create_spec(tmp_path, arch=arch, name=f"bind-{backend.value}")
    intent = RunVolumeIntent("data", "/srv/data")
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, backend),
            logical,
            (intent,),
            attachments_bound=False,
        )
    )
    if attachment_kind == "backend-name":
        attachment = VolumeAttachment("data", "/srv/data", backend_name="data-disk")
    else:
        host_path = tmp_path / f"{backend.value}.raw"
        host_path.write_bytes(b"data")
        attachment = VolumeAttachment("data", "/srv/data", host_path=host_path)

    with pytest.raises(StateError, match="source does not match resolved backend"):
        _bind_authorized(
            request,
            RunSpec(**{**logical.__dict__, "volumes": (attachment,)}),
            dispatch_key=request.dispatch_key,
        )


def test_public_volume_binder_rejects_arbitrary_same_backend_source_without_verifier_receipt(
    tmp_path: Path,
) -> None:
    logical_spec = _create_spec(tmp_path)
    intent = RunVolumeIntent("data", "/srv/data")
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            logical_spec,
            (intent,),
            attachments_bound=False,
        )
    )
    attacker_path = tmp_path / "attacker-controlled.raw"
    attacker_path.write_bytes(b"attacker")
    attacker_spec = replace(
        logical_spec,
        volumes=(VolumeAttachment("data", "/srv/data", host_path=attacker_path),),
    )

    with pytest.raises(StateError, match="verifier-issued receipt"):
        runtime_dispatch.bind_run_request_volumes(
            request,
            attacker_spec,
            dispatch_key=request.dispatch_key,
        )

    lima_spec = _create_spec(tmp_path, arch="aarch64", name="lima-attacker")
    lima_request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
            lima_spec,
            (intent,),
            attachments_bound=False,
        )
    )
    formatted_attacker_spec = replace(
        lima_spec,
        volumes=(VolumeAttachment("data", "/srv/data", backend_name="attacker-disk", format=True),),
    )
    with pytest.raises(StateError, match="verifier-issued receipt"):
        runtime_dispatch.bind_run_request_volumes(
            lima_request,
            formatted_attacker_spec,
            dispatch_key=lima_request.dispatch_key,
        )


def test_bound_volume_source_substitution_invalidates_request_provenance(tmp_path: Path) -> None:
    logical_spec = _create_spec(tmp_path)
    intent = RunVolumeIntent("data", "/srv/data")
    logical = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            logical_spec,
            (intent,),
            attachments_bound=False,
        )
    )
    managed = tmp_path / "managed.raw"
    attacker = tmp_path / "attacker.raw"
    managed.write_bytes(b"managed")
    attacker.write_bytes(b"attacker")
    bound = _bind_authorized(
        logical,
        replace(logical_spec, volumes=(VolumeAttachment("data", "/srv/data", host_path=managed),)),
        dispatch_key=logical.dispatch_key,
    )
    substituted = replace(
        bound,
        spec=replace(
            bound.spec,
            volumes=(VolumeAttachment("data", "/srv/data", host_path=attacker),),
        ),
    )

    with pytest.raises(RuntimePreflightError) as captured:
        runtime_dispatch.preflight_run_request(substituted)

    assert captured.value.category is CapabilityErrorCategory.REPORT_PROVENANCE


def test_volume_binding_receipts_prune_abandoned_issue_and_expire_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_spec = _create_spec(tmp_path)
    intent = RunVolumeIntent("data", "/srv/data")
    logical = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            logical_spec,
            (intent,),
            attachments_bound=False,
        )
    )
    managed = tmp_path / "managed.raw"
    managed.write_bytes(b"managed")
    final_spec = replace(
        logical_spec,
        volumes=(VolumeAttachment("data", "/srv/data", host_path=managed),),
    )
    abandoned = runtime_dispatch._issue_volume_binding_receipt(
        logical,
        final_spec,
        dispatch_key=logical.dispatch_key,
        _authority=runtime_dispatch._PROJECT_VOLUME_BINDING_AUTHORITY,
        now_ns=0,
    )
    expiring = runtime_dispatch._issue_volume_binding_receipt(
        logical,
        final_spec,
        dispatch_key=logical.dispatch_key,
        _authority=runtime_dispatch._PROJECT_VOLUME_BINDING_AUTHORITY,
        now_ns=abandoned.expires_at_monotonic_ns,
    )
    assert abandoned.issuer_nonce not in runtime_dispatch._ISSUED_VOLUME_BINDING_RECEIPTS
    monkeypatch.setattr(runtime_dispatch.time, "monotonic_ns", lambda: expiring.expires_at_monotonic_ns)

    with pytest.raises(StateError, match="expired"):
        runtime_dispatch.bind_run_request_volumes(
            logical,
            final_spec,
            dispatch_key=logical.dispatch_key,
            receipt=expiring,
        )

    assert expiring.issuer_nonce not in runtime_dispatch._ISSUED_VOLUME_BINDING_RECEIPTS


def test_abandoned_volume_binding_receipts_have_a_hard_outstanding_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_spec = _create_spec(tmp_path)
    logical = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            logical_spec,
            (RunVolumeIntent("data", "/srv/data"),),
            attachments_bound=False,
        )
    )
    managed = tmp_path / "managed-cap.raw"
    managed.write_bytes(b"managed")
    final_spec = replace(
        logical_spec,
        volumes=(VolumeAttachment("data", "/srv/data", host_path=managed),),
    )
    monkeypatch.setattr(runtime_dispatch, "_MAX_ISSUED_VOLUME_BINDING_RECEIPTS", 2)
    with runtime_dispatch._VOLUME_BINDING_RECEIPT_LOCK:
        saved = dict(runtime_dispatch._ISSUED_VOLUME_BINDING_RECEIPTS)
        runtime_dispatch._ISSUED_VOLUME_BINDING_RECEIPTS.clear()
    try:
        for _ in range(2):
            runtime_dispatch._issue_volume_binding_receipt(
                logical,
                final_spec,
                dispatch_key=logical.dispatch_key,
                _authority=runtime_dispatch._PROJECT_VOLUME_BINDING_AUTHORITY,
                now_ns=0,
            )
        with pytest.raises(StateError, match="capacity is exhausted"):
            runtime_dispatch._issue_volume_binding_receipt(
                logical,
                final_spec,
                dispatch_key=logical.dispatch_key,
                _authority=runtime_dispatch._PROJECT_VOLUME_BINDING_AUTHORITY,
                now_ns=0,
            )
        with runtime_dispatch._VOLUME_BINDING_RECEIPT_LOCK:
            assert len(runtime_dispatch._ISSUED_VOLUME_BINDING_RECEIPTS) == 2
    finally:
        with runtime_dispatch._VOLUME_BINDING_RECEIPT_LOCK:
            runtime_dispatch._ISSUED_VOLUME_BINDING_RECEIPTS.clear()
            runtime_dispatch._ISSUED_VOLUME_BINDING_RECEIPTS.update(saved)


def test_volume_binder_rejects_invalid_dispatch_and_attachment_types_with_stable_errors(tmp_path: Path) -> None:
    logical_spec = _create_spec(tmp_path)
    logical = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            logical_spec,
            (RunVolumeIntent("data", "/srv/data"),),
            attachments_bound=False,
        )
    )
    with pytest.raises(StateError, match="dispatch identity"):
        runtime_dispatch.bind_run_request_volumes(
            logical,
            logical_spec,
            dispatch_key="kvm",  # type: ignore[arg-type]
        )

    malformed = replace(logical_spec)
    object.__setattr__(malformed, "volumes", (object(),))
    with pytest.raises(StateError, match="attachments are invalid"):
        runtime_dispatch.bind_run_request_volumes(
            logical,
            malformed,
            dispatch_key=logical.dispatch_key,
        )


def test_cloud_init_is_snapshotted_before_preflight_and_cannot_change_before_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud_init = SimpleNamespace(packages=["curl"], write_files=[], runcmd=[])
    spec = replace(_create_spec(tmp_path), cloud_init=cloud_init)
    monkeypatch.setattr(runtime_dispatch.platforms, "select_backend", lambda *_args, **_kwargs: "kvm")
    request = runtime_dispatch.resolve_run_request(spec)
    assert isinstance(request.spec.cloud_init, CloudInitSnapshot)
    assert request.spec.cloud_init.packages == ("curl",)
    report = runtime_dispatch.preflight_run_request(request)
    cloud_init.packages.append("git")
    roots = _roots(tmp_path / "roots")
    consumed = runtime_dispatch.platforms.consume_capability_report
    observed: list[object] = []

    def consume_then_mutate(*args: object, **kwargs: object) -> None:
        consumed(*args, **kwargs)  # type: ignore[arg-type]
        cloud_init.packages.append("wget")

    def adapter(adapter_spec: RunSpec, **_kwargs: object) -> dict[str, str]:
        observed.append(adapter_spec.cloud_init)
        _rpaths, run_id = _write_ledger(
            roots,
            name=adapter_spec.name,
            record={
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": "kvm",
                "status": "running",
                "guest_ip": "192.0.2.25",
            },
        )
        return {
            "name": adapter_spec.name,
            "run_id": run_id,
            "backend": "kvm",
            "status": "running",
        }

    monkeypatch.setattr(runtime_dispatch.platforms, "consume_capability_report", consume_then_mutate)
    monkeypatch.setattr(runtime_dispatch.platforms, "resolve_domain_profile", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "run", adapter)

    runtime_dispatch.run(request, preflight=report, roots=roots)

    assert observed == [CloudInitSnapshot(("curl",), (), ())]
    assert cloud_init.packages == ["curl", "git", "wget"]


def test_cloud_init_getter_failure_does_not_escape_secret_in_exception_graph_or_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ATTACKER-CLOUD-INIT-SECRET"

    class MaliciousCloudInit:
        @property
        def packages(self) -> object:
            raise RuntimeError(secret)

    monkeypatch.setattr(runtime_dispatch.platforms, "select_backend", lambda *_args, **_kwargs: "kvm")
    with pytest.raises(TypeError) as captured:
        runtime_dispatch.resolve_run_request(replace(_create_spec(tmp_path), cloud_init=MaliciousCloudInit()))

    error = captured.value
    rendered = "".join(traceback.format_exception(error))
    assert str(error) == "runtime cloud-init input cannot be converted to an immutable snapshot"
    assert secret not in repr(error)
    assert secret not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("control_error", [KeyboardInterrupt, SystemExit])
def test_cloud_init_snapshot_propagates_process_control_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_error: type[BaseException],
) -> None:
    class InterruptingCloudInit:
        @property
        def packages(self) -> object:
            raise control_error()

    monkeypatch.setattr(runtime_dispatch.platforms, "select_backend", lambda *_args, **_kwargs: "kvm")
    with pytest.raises(control_error):
        runtime_dispatch.resolve_run_request(replace(_create_spec(tmp_path), cloud_init=InterruptingCloudInit()))


@pytest.mark.parametrize(
    ("backend", "arch", "adapter"),
    [
        (RuntimeBackend.KVM, "x86_64", "cloud"),
        (RuntimeBackend.LIBVIRT_HVF, "aarch64", "cloud"),
        (RuntimeBackend.LIMA_VZ, "aarch64", "lima"),
    ],
)
def test_create_dispatch_uses_only_the_resolved_backend_without_reselection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: RuntimeBackend,
    arch: str,
    adapter: str,
) -> None:
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, backend),
            _create_spec(tmp_path, arch=arch, name=f"create-{backend.value}"),
        )
    )
    calls: list[tuple[str, object]] = []
    profile = object()
    roots = _roots(tmp_path / "roots")

    def write_success(spec: RunSpec, selected_backend: RuntimeBackend) -> dict[str, str]:
        _rpaths, run_id = _write_ledger(
            roots,
            name=spec.name,
            record={
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": selected_backend.value,
                "status": "running",
                "guest_ip": "192.0.2.10",
                "base": {"local_path": "/private/SENSITIVE_VALUE/base.qcow2"},
                "volumes": [{"host_path": "/private/SENSITIVE_VALUE/data.raw"}],
            },
        )
        return {
            "name": spec.name,
            "run_id": run_id,
            "backend": selected_backend.value,
            "status": "running",
            "guest_ip": "198.51.100.200",
            "local_path": "/private/SENSITIVE_VALUE/root.raw",
        }

    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "select_backend",
        lambda *_args, **_kwargs: pytest.fail("dispatcher reselected backend"),
    )
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "preflight",
        lambda *_args, **_kwargs: pytest.fail("dispatcher repeated caller-owned preflight"),
    )
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "resolve_domain_profile",
        lambda selected, selected_arch: calls.append(("profile", (selected, selected_arch))) or profile,
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "run",
        lambda spec, roots=None, profile=None: (
            calls.append(("cloud", (spec, roots, profile))) or write_success(spec, backend)
        ),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        "run",
        lambda spec, roots=None: calls.append(("lima", (spec, roots))) or write_success(spec, backend),
    )

    result = runtime_dispatch.run(request, preflight=_successful_run_preflight(request), roots=roots)

    assert isinstance(result, RunResult)
    assert result.name == request.spec.name
    assert result.backend is backend
    assert result.runtime_kind is RuntimeKind.CLOUD_IMAGE
    assert result.status == "running"
    assert result.ready is True
    assert result.attachment_mode is RunAttachmentMode.DETACHED
    assert result.guest_ip == "192.0.2.10"
    assert result.session is None
    assert "SENSITIVE_VALUE" not in repr(result)
    assert "local_path" not in repr(result)
    assert [name for name, _value in calls if name in {"cloud", "lima"}] == [adapter]
    if adapter == "cloud":
        assert calls[0] == ("profile", (backend.value, arch))
        assert calls[1][1][2] is profile  # type: ignore[index]
    else:
        assert all(name != "profile" for name, _value in calls)


@pytest.mark.parametrize("field", ["name", "run_id", "backend", "status"])
def test_malformed_creation_receipt_never_retains_attacker_values_in_exception(field: str) -> None:
    raw = {
        "name": "demo",
        "run_id": "862ffb44-6795-4618-b2d8-c0750439fac3",
        "backend": "kvm",
        "status": "running",
    }
    raw[field] = "SENSITIVE_VALUE"

    with pytest.raises(StateError) as exc_info:
        runtime_dispatch._parse_creation_receipt(raw)

    error = exc_info.value
    assert str(error) == "runtime adapter returned an invalid creation receipt"
    assert "SENSITIVE_VALUE" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_create_dispatch_rejects_unbound_project_request_before_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            _create_spec(tmp_path),
            (RunVolumeIntent("data", "/srv/data"),),
            attachments_bound=False,
        )
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unbound request entered adapter"),
    )
    with pytest.raises(StateError, match="volumes have not been prepared"):
        runtime_dispatch.run(
            request,
            preflight=_successful_run_preflight(request),
            roots=_roots(tmp_path / "roots"),
        )


@pytest.mark.parametrize(
    ("ledger_backend", "ledger_status", "message"),
    [
        ("lima-vz", "running", "identity does not match"),
        ("kvm", "stopped", "did not reach running"),
    ],
)
def test_create_dispatch_rejects_mismatched_or_not_running_post_read_ledgers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_backend: str,
    ledger_status: str,
    message: str,
) -> None:
    roots = _roots(tmp_path)
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            _create_spec(tmp_path),
        )
    )

    def adapter(spec: RunSpec, **_kwargs: object) -> dict[str, str]:
        _rpaths, run_id = _write_ledger(
            roots,
            name=spec.name,
            record={
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": ledger_backend,
                "status": ledger_status,
            },
        )
        return {
            "name": spec.name,
            "run_id": run_id,
            "backend": "kvm",
            "status": "running",
            "local_path": "/private/SENSITIVE_VALUE/root.raw",
        }

    monkeypatch.setattr(runtime_dispatch.platforms, "resolve_domain_profile", lambda *_args: object())
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "run", adapter)

    with pytest.raises(StateError, match=message) as exc_info:
        runtime_dispatch.run(request, preflight=_successful_run_preflight(request), roots=roots)
    assert "SENSITIVE_VALUE" not in str(exc_info.value)


@pytest.mark.parametrize("attack", ["replace-after-receipt", "pre-existing-ledger"])
def test_creation_receipt_rejects_same_name_backend_ledger_identity_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    roots = _roots(tmp_path)
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            _create_spec(tmp_path, name="receipt-attack"),
        )
    )
    receipt_run_id = str(uuid.uuid4())
    rpaths = state.run_paths(roots, request.spec.name)
    if attack == "pre-existing-ledger":
        rpaths, _preexisting_run_id = _write_ledger(
            roots,
            name=request.spec.name,
            record={"backend": "kvm", "status": "running"},
        )

    def adapter(_spec: RunSpec, **_kwargs: object) -> dict[str, str]:
        nonlocal rpaths
        if attack == "replace-after-receipt":
            rpaths, _created_run_id = _write_ledger(
                roots,
                name=request.spec.name,
                owner={"schema_version": 1, "run_id": receipt_run_id, "name": request.spec.name},
                record={"backend": "kvm", "status": "running"},
            )
            replacement_run_id = str(uuid.uuid4())
            rpaths.owner.write_text(
                json.dumps({"schema_version": 1, "run_id": replacement_run_id, "name": request.spec.name}) + "\n",
                encoding="utf-8",
            )
            rpaths.state.write_text(
                json.dumps(
                    {
                        "name": request.spec.name,
                        "run_id": replacement_run_id,
                        "backend": "kvm",
                        "status": "running",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        return {
            "name": request.spec.name,
            "run_id": receipt_run_id,
            "backend": "kvm",
            "status": "running",
        }

    monkeypatch.setattr(runtime_dispatch.platforms, "resolve_domain_profile", lambda *_args: object())
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "run", adapter)

    with pytest.raises(StateError, match="ledger identity does not match"):
        runtime_dispatch.run(request, preflight=_successful_run_preflight(request), roots=roots)


def test_create_dispatch_fails_closed_when_adapter_does_not_leave_a_readable_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    request = _authorized_request(
        ResolvedRunRequest(
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            _create_spec(tmp_path),
        )
    )
    monkeypatch.setattr(runtime_dispatch.platforms, "resolve_domain_profile", lambda *_args: object())
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "run",
        lambda spec, **_kwargs: {
            "name": spec.name,
            "run_id": str(uuid.uuid4()),
            "backend": "kvm",
            "status": "running",
            "guest_ip": "192.0.2.20",
        },
    )

    with pytest.raises(StateError, match="securely read run ledger"):
        runtime_dispatch.run(request, preflight=_successful_run_preflight(request), roots=roots)


def test_run_result_public_constructor_validates_safe_immutable_fields() -> None:
    record = ExistingRunRecord(
        name="demo",
        run_id="862ffb44-6795-4618-b2d8-c0750439fac3",
        state_schema_version=2,
        dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    result = RunResult(record, "running", True, RunAttachmentMode.DETACHED, "192.0.2.10")

    assert result.name == "demo"
    assert result.run_id == record.run_id
    with pytest.raises(FrozenInstanceError):
        result.ready = False  # type: ignore[misc]
    with pytest.raises(ValueError, match="readiness"):
        RunResult(record, "running", False, RunAttachmentMode.DETACHED, None)
    with pytest.raises(ValueError, match="guest IP"):
        RunResult(record, "running", True, RunAttachmentMode.DETACHED, "/private/SENSITIVE_VALUE")


@pytest.mark.parametrize("status", ["running", "exited"])
def test_oci_run_result_attached_session_is_private_and_exit_remains_session_owned(status: str) -> None:
    record = ExistingRunRecord(
        "oci-demo",
        "862ffb44-6795-4618-b2d8-c0750439fac3",
        2,
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )

    class SecretSession(_FakeProcessSession):
        def __repr__(self) -> str:
            return "<session SENSITIVE_SESSION_TOKEN>"

    session = SecretSession()
    result = RunResult(record, status, True, RunAttachmentMode.ATTACHED, session=session)
    equivalent = RunResult(record, status, True, RunAttachmentMode.ATTACHED, session=SecretSession())

    assert result.session is session
    assert result.launch_hint is None
    assert result == equivalent
    assert hash(result) == hash(equivalent)
    assert "SENSITIVE_SESSION_TOKEN" not in repr(result)
    assert not hasattr(result, "exit")


def test_run_result_attachment_readiness_matrix_and_backend_neutral_projection() -> None:
    cloud = ExistingRunRecord(
        "cloud-demo",
        "862ffb44-6795-4618-b2d8-c0750439fac3",
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
    )
    lima_record = ExistingRunRecord(
        "lima-demo",
        "962ffb44-6795-4618-b2d8-c0750439fac3",
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
    )
    oci = ExistingRunRecord(
        "oci-demo",
        "a62ffb44-6795-4618-b2d8-c0750439fac3",
        2,
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )

    cloud_result = RunResult(cloud, "running", True, RunAttachmentMode.DETACHED, None)
    assert cloud_result.launch_hint is None
    assert cloud_result.warnings == (RunWarningCategory.EXPERIMENTAL_BACKEND,)
    assert RunResult(lima_record, "running", True, RunAttachmentMode.DETACHED).launch_hint == "limactl shell lima-demo"
    assert RunResult(oci, "failed", False, RunAttachmentMode.DETACHED).launch_hint is None
    with pytest.raises(ValueError, match="process session"):
        RunResult(oci, "running", True, RunAttachmentMode.DETACHED, session=_FakeProcessSession())
    with pytest.raises(ValueError, match="ready OCI-root"):
        RunResult(cloud, "running", True, RunAttachmentMode.ATTACHED, session=_FakeProcessSession())
    with pytest.raises(ValueError, match="readiness"):
        RunResult(oci, "starting", True, RunAttachmentMode.DETACHED)
    with pytest.raises(ValueError, match="readiness"):
        RunResult(oci, "failed", True, RunAttachmentMode.DETACHED)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Demo"},
        {"run_id": "not-a-uuid"},
        {"run_id": "862FFB44-6795-4618-B2D8-C0750439FAC3"},
        {"state_schema_version": True},
        {"state_schema_version": 0},
        {"state_schema_version": 3},
        {"dispatch_key": "cloud-image/kvm"},
    ],
)
def test_existing_run_record_public_constructor_enforces_every_invariant(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "name": "demo",
        "run_id": "862ffb44-6795-4618-b2d8-c0750439fac3",
        "state_schema_version": 2,
        "dispatch_key": DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        **kwargs,
    }
    with pytest.raises((TypeError, ValueError)):
        ExistingRunRecord(**values)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Demo"},
        {"run_id": "not-a-uuid"},
        {"run_id": "862FFB44-6795-4618-B2D8-C0750439FAC3"},
        {"dispatch_key": "cloud-image/kvm"},
    ],
)
def test_expected_run_identity_public_constructor_enforces_every_invariant(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "name": "demo",
        "run_id": "862ffb44-6795-4618-b2d8-c0750439fac3",
        "dispatch_key": DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        **kwargs,
    }
    with pytest.raises((TypeError, ValueError)):
        ExpectedRunIdentity(**values)


def test_missing_run_resolution_does_not_create_state_or_config_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config-home"
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    with pytest.raises(StateError):
        runtime_dispatch.resolve_existing_run("missing")

    assert not config_home.exists()
    assert not state_home.exists()


def test_legacy_state_may_omit_both_identity_fields_without_being_rewritten(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _rpaths, run_id = _write_ledger(roots, record={"status": "stopped"})
    before = _snapshot_tree(roots.state)

    record = state.read_run_dispatch_record(roots, "demo")

    assert record.run_id == run_id
    assert record.state_schema_version == 1
    assert record.dispatch_key == DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    ("schema_version", "runtime_kind", "backend", "expected_kind", "expected_backend"),
    [
        (None, None, None, RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        (1, None, None, RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        (1, None, "libvirt-hvf", RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
        (1, "cloud-image", "lima-vz", RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
        (2, "cloud-image", "kvm", RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        (2, "oci-root", "kvm", RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    ],
)
def test_read_run_dispatch_record_normalizes_only_durable_schema_rules(
    tmp_path: Path,
    schema_version: int | None,
    runtime_kind: str | None,
    backend: str | None,
    expected_kind: RuntimeKind,
    expected_backend: RuntimeBackend,
) -> None:
    roots = _roots(tmp_path)
    record: dict[str, Any] = {"name": "demo", "status": "stopped"}
    if schema_version is not None:
        record["schema_version"] = schema_version
    if runtime_kind is not None:
        record["runtime_kind"] = runtime_kind
    if backend is not None:
        record["backend"] = backend
    _rpaths, run_id = _write_ledger(roots, record=record)
    before = _snapshot_tree(roots.state)

    resolved = state.read_run_dispatch_record(roots, "demo")

    assert resolved.name == "demo"
    assert resolved.run_id == run_id
    assert resolved.state_schema_version == (1 if schema_version is None else schema_version)
    assert resolved.dispatch_key == DispatchKey(expected_kind, expected_backend)
    assert runtime_dispatch.resolve_existing_run("demo", roots=roots) == resolved
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    "record",
    [
        {"schema_version": True, "runtime_kind": "cloud-image", "backend": "kvm"},
        {"schema_version": 0, "runtime_kind": "cloud-image", "backend": "kvm"},
        {"schema_version": 3, "runtime_kind": "cloud-image", "backend": "kvm"},
        {"schema_version": 2, "backend": "kvm"},
        {"schema_version": 2, "runtime_kind": "cloud-image"},
        {"schema_version": 2, "runtime_kind": "unknown", "backend": "kvm"},
        {"schema_version": 2, "runtime_kind": "cloud-image", "backend": "unknown"},
        {"schema_version": 2, "runtime_kind": "oci-root", "backend": "lima-vz"},
        {"schema_version": 1, "runtime_kind": "oci-root", "backend": "kvm"},
    ],
)
def test_read_run_dispatch_record_rejects_unknown_or_ambiguous_state_without_rewrite(
    tmp_path: Path,
    record: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _rpaths, _ = _write_ledger(roots, record={"name": "demo", "status": "stopped", **record})
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError):
        state.read_run_dispatch_record(roots, "demo")

    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    "owner_update",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"schema_version": "1"},
        {"run_id": "not-a-uuid"},
        {"run_id": "862FFB44-6795-4618-B2D8-C0750439FAC3"},
        {"run_id": "862ffb4467954618b2d8c0750439fac3"},
        {"name": "other"},
        {"name": "Demo"},
    ],
)
def test_read_run_dispatch_record_rejects_invalid_owner_identity(
    tmp_path: Path,
    owner_update: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    owner = {"schema_version": 1, "run_id": str(uuid.uuid4()), "name": "demo", **owner_update}
    _rpaths, _ = _write_ledger(roots, owner=owner, record={"name": "demo", "status": "stopped"})
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError):
        state.read_run_dispatch_record(roots, "demo")

    assert _snapshot_tree(roots.state) == before


def test_read_run_dispatch_record_rejects_conflicting_state_identity(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _rpaths, _ = _write_ledger(
        roots,
        record={"name": "demo", "run_id": str(uuid.uuid4()), "status": "stopped"},
    )
    before = _snapshot_tree(roots.state)
    with pytest.raises(StateError, match="state run_id mismatch"):
        state.read_run_dispatch_record(roots, "demo")
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    "record",
    [
        {"schema_version": 1, "name": None},
        {"schema_version": 1, "run_id": None},
        {"schema_version": 1, "name": "other"},
        {"schema_version": 1, "run_id": "862ffb44-6795-4618-b2d8-c0750439fac3"},
        {"schema_version": 2, "runtime_kind": "cloud-image", "backend": "kvm"},
        {"schema_version": 2, "runtime_kind": "cloud-image", "backend": "kvm", "name": None},
        {"schema_version": 2, "runtime_kind": "cloud-image", "backend": "kvm", "run_id": None},
    ],
)
def test_state_identity_uses_explicit_legacy_compatibility_and_strict_v2_rules(
    tmp_path: Path,
    record: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _rpaths, _ = _write_ledger(roots, record=record, complete_v2_identity=False)
    before = _snapshot_tree(roots.state)
    with pytest.raises(StateError):
        state.read_run_dispatch_record(roots, "demo")
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize("filename", ["owner.json", "state.json"])
def test_dispatch_reader_rejects_symlinked_ledger_files(tmp_path: Path, filename: str) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    target = rpaths.root / f"real-{filename}"
    (rpaths.root / filename).rename(target)
    (rpaths.root / filename).symlink_to(target.name)
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely read run ledger") as captured:
        state.read_run_dispatch_record(roots, "demo")

    rendered = f"{captured.value!s} {captured.value!r}"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert filename not in rendered
    assert str(tmp_path) not in rendered
    assert "Errno" not in rendered
    assert _snapshot_tree(roots.state) == before


def test_secure_run_presence_returns_false_only_for_stable_enoent_without_writes(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    before = _snapshot_tree(roots.state)

    assert state.run_entry_present_or_ambiguous(roots, "demo") is False
    assert _snapshot_tree(roots.state) == before

    roots.runs.rmdir()
    before_missing_parent = _snapshot_tree(roots.state)
    assert state.run_entry_present_or_ambiguous(roots, "demo") is False
    assert _snapshot_tree(roots.state) == before_missing_parent


@pytest.mark.parametrize("entry_kind", ["directory", "dangling-symlink", "file"])
def test_secure_run_presence_treats_every_name_entry_as_present_or_ambiguous(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    roots = _roots(tmp_path)
    entry = roots.runs / "demo"
    if entry_kind == "directory":
        entry.mkdir()
    elif entry_kind == "dangling-symlink":
        entry.symlink_to("missing-run", target_is_directory=True)
    else:
        entry.write_text("not a directory", encoding="utf-8")
    before = _snapshot_tree(roots.state)

    assert state.run_entry_present_or_ambiguous(roots, "demo") is True
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize("parent_kind", ["symlink", "non-directory"])
def test_secure_run_presence_rejects_ambiguous_runs_parent_without_context_or_writes(
    tmp_path: Path,
    parent_kind: str,
) -> None:
    roots = _roots(tmp_path)
    if parent_kind == "symlink":
        real_runs = roots.state / "real-runs"
        roots.runs.rename(real_runs)
        roots.runs.symlink_to(real_runs.name, target_is_directory=True)
    else:
        roots.runs.rmdir()
        roots.runs.write_text("not a directory", encoding="utf-8")
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely inspect run entry") as captured:
        state.run_entry_present_or_ambiguous(roots, "demo")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _snapshot_tree(roots.state) == before


def test_secure_run_presence_rejects_permission_error_without_reflecting_os_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    original_open = state.os.open

    def denied_open(path: str | os.PathLike[str], flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == roots.runs and kwargs.get("dir_fd") is None:
            raise PermissionError(13, "SENSITIVE_PERMISSION_DETAIL", str(path))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(state.os, "open", denied_open)
    with pytest.raises(StateError, match="cannot securely inspect run entry") as captured:
        state.run_entry_present_or_ambiguous(roots, "demo")

    rendered = f"{captured.value!s} {captured.value!r}"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SENSITIVE_" not in rendered
    assert str(roots.runs) not in rendered


def test_secure_run_presence_rejects_runs_parent_swap_while_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    original_fstat = state._safe_fstat
    calls = 0

    def swapping_fstat(file_fd: int) -> os.stat_result | None:
        nonlocal calls
        result = original_fstat(file_fd)
        calls += 1
        if calls == 1:
            roots.runs.rename(roots.state / "old-runs")
            roots.runs.mkdir()
        return result

    monkeypatch.setattr(state, "_safe_fstat", swapping_fstat)
    with pytest.raises(StateError, match="cannot securely inspect run entry") as captured:
        state.run_entry_present_or_ambiguous(roots, "demo")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("filename", ["owner.json", "state.json"])
@pytest.mark.parametrize("node_kind", ["fifo", "unix-socket"])
def test_dispatch_reader_rejects_nonregular_ledger_without_blocking_writing_or_leaking_fds(
    tmp_path: Path,
    filename: str,
    node_kind: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    ledger_path = rpaths.root / filename
    ledger_path.unlink()
    socket_holder: socket.socket | None = None
    short_socket_root: Path | None = None
    short_run_alias: Path | None = None

    try:
        if node_kind == "fifo":
            os.mkfifo(ledger_path, mode=0o600)
        else:
            short_socket_root = Path(tempfile.mkdtemp(prefix="pali-sock-", dir="/tmp"))
            short_run_alias = short_socket_root / "run"
            short_run_alias.symlink_to(rpaths.root, target_is_directory=True)
            socket_holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            socket_holder.bind(str(short_run_alias / filename))
        before = _snapshot_tree(roots.state)
        result = _run_dispatch_reader_subprocess(roots)
        after = _snapshot_tree(roots.state)
    finally:
        if socket_holder is not None:
            socket_holder.close()
        if short_run_alias is not None:
            short_run_alias.unlink()
        if short_socket_root is not None:
            short_socket_root.rmdir()

    assert (result.returncode, result.stdout, result.stderr) == (0, "ok\n", "")
    assert after == before


@pytest.mark.parametrize("filename", ["owner.json", "state.json"])
def test_dispatch_reader_rejects_regular_to_fifo_swap_without_blocking_or_leaking_fds(
    tmp_path: Path,
    filename: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    untouched_path = rpaths.state if filename == "owner.json" else rpaths.owner
    untouched = untouched_path.read_bytes()
    setup_source = f"""
target = {filename!r}
original_open = state._open_readonly_no_follow
swapped = False

def racing_open(path, *, directory_fd=None, directory=False, nonblocking=False):
    global swapped
    if path == target and directory_fd is not None and not directory and not swapped:
        swapped = True
        os.unlink(path, dir_fd=directory_fd)
        os.mkfifo(path, mode=0o600, dir_fd=directory_fd)
    return original_open(
        path,
        directory_fd=directory_fd,
        directory=directory,
        nonblocking=nonblocking,
    )

state._open_readonly_no_follow = racing_open
"""

    result = _run_dispatch_reader_subprocess(roots, setup_source=setup_source)

    assert (result.returncode, result.stdout, result.stderr) == (0, "ok\n", "")
    assert stat_module.S_ISFIFO((rpaths.root / filename).lstat().st_mode)
    assert untouched_path.read_bytes() == untouched


def test_dispatch_reader_rejects_a_symlinked_run_directory(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    backing = roots.runs / "backing"
    backing.mkdir()
    run_id = str(uuid.uuid4())
    (backing / "owner.json").write_text(
        json.dumps({"schema_version": 1, "run_id": run_id, "name": "demo"}) + "\n",
        encoding="utf-8",
    )
    (backing / "state.json").write_text(json.dumps({"status": "stopped"}) + "\n", encoding="utf-8")
    (roots.runs / "demo").symlink_to(backing.name, target_is_directory=True)
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely read run ledger"):
        state.read_run_dispatch_record(roots, "demo")

    assert _snapshot_tree(roots.state) == before


def test_dispatch_reader_rejects_a_symlinked_runs_parent(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _write_ledger(roots)
    real_runs = roots.state / "real-runs"
    roots.runs.rename(real_runs)
    roots.runs.symlink_to(real_runs.name, target_is_directory=True)
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely read run ledger") as captured:
        state.read_run_dispatch_record(roots, "demo")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _snapshot_tree(roots.state) == before


def test_dispatch_reader_rejects_runs_parent_swap_during_pinned_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(roots)
    original_reader = state._read_pinned_json_object
    calls = 0

    def swapping_reader(directory_fd: int, filename: str) -> dict[str, Any]:
        nonlocal calls
        result = original_reader(directory_fd, filename)
        calls += 1
        if calls == 1:
            roots.runs.rename(roots.state / "old-runs")
            roots.runs.mkdir()
            _write_ledger(roots)
        return result

    monkeypatch.setattr(state, "_read_pinned_json_object", swapping_reader)
    with pytest.raises(StateError, match="run ledger changed during read"):
        state.read_run_dispatch_record(roots, "demo")


@pytest.mark.parametrize("mutation", ["directory", "oversized", "array", "malformed", "unicode", "recursive"])
def test_dispatch_reader_requires_bounded_regular_json_objects(tmp_path: Path, mutation: str) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    rpaths.state.unlink()
    if mutation == "directory":
        rpaths.state.mkdir()
    elif mutation == "oversized":
        rpaths.state.write_bytes(b"{" + b" " * (1024 * 1024) + b"}")
    elif mutation == "array":
        rpaths.state.write_text("[]\n", encoding="utf-8")
    elif mutation == "malformed":
        rpaths.state.write_text('{"value":"SENSITIVE_MALFORMED_JSON"', encoding="utf-8")
    elif mutation == "unicode":
        rpaths.state.write_bytes(b'{"value":"SENSITIVE_UNICODE_\xff"}')
    else:
        rpaths.state.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely read run ledger") as captured:
        state.read_run_dispatch_record(roots, "demo")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SENSITIVE_" not in f"{captured.value!s} {captured.value!r}"
    assert _snapshot_tree(roots.state) == before


def test_dispatch_reader_rejects_run_directory_swap_during_pinned_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    original_reader = state._read_pinned_json_object
    calls = 0

    def swapping_reader(directory_fd: int, filename: str) -> dict[str, Any]:
        nonlocal calls
        result = original_reader(directory_fd, filename)
        calls += 1
        if calls == 1:
            rpaths.root.rename(roots.runs / "old-demo")
            _write_ledger(roots)
        return result

    monkeypatch.setattr(state, "_read_pinned_json_object", swapping_reader)
    with pytest.raises(StateError, match="run ledger changed during read"):
        state.read_run_dispatch_record(roots, "demo")


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        (
            "state",
            {"schema_version": "SENSITIVE_SCHEMA_VALUE", "name": "demo", "status": "stopped"},
        ),
        (
            "state",
            {
                "schema_version": 2,
                "runtime_kind": "SENSITIVE_RUNTIME_KIND",
                "backend": "kvm",
                "name": "demo",
                "status": "stopped",
            },
        ),
        (
            "owner",
            {"schema_version": 1, "run_id": "SENSITIVE_OWNER_UUID", "name": "demo"},
        ),
    ],
)
def test_dispatch_validation_never_reflects_untrusted_values_or_causes(
    tmp_path: Path,
    target: str,
    payload: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    owner = payload if target == "owner" else None
    record = payload if target == "state" else None
    _write_ledger(roots, owner=owner, record=record, complete_v2_identity=False)

    with pytest.raises(StateError) as captured:
        state.read_run_dispatch_record(roots, "demo")

    error: BaseException | None = captured.value
    rendered: list[str] = []
    while error is not None:
        rendered.append(f"{error!s} {error!r}")
        assert error.__context__ is None
        error = error.__cause__
    assert "SENSITIVE_" not in " ".join(rendered)


_OPERATIONS: tuple[tuple[RuntimeOperation, str, Callable[..., Any], dict[str, Any]], ...] = (
    (RuntimeOperation.START, "start", runtime_dispatch.start, {}),
    (RuntimeOperation.STOP, "stop", runtime_dispatch.stop, {}),
    (RuntimeOperation.RM, "rm", runtime_dispatch.rm, {"volumes": True}),
    (RuntimeOperation.INSPECT, "inspect_run", runtime_dispatch.inspect_run, {}),
)

_ADAPTER_ENTRY_OPERATIONS: tuple[tuple[RuntimeOperation, Callable[..., Any], dict[str, Any]], ...] = (
    (RuntimeOperation.START, runtime_dispatch.start, {}),
    (RuntimeOperation.STOP, runtime_dispatch.stop, {}),
    (RuntimeOperation.RM, runtime_dispatch.rm, {"volumes": True}),
    (RuntimeOperation.INSPECT, runtime_dispatch.inspect_run, {}),
)


@pytest.mark.parametrize(
    ("target_name", "dispatch", "kwargs"),
    [
        ("start", runtime_dispatch.start, {}),
        ("stop", runtime_dispatch.stop, {}),
        ("rm", runtime_dispatch.rm, {"volumes": True}),
    ],
)
@pytest.mark.parametrize("mismatch", ["run-id", "backend", "runtime-kind"])
def test_expected_project_identity_rejects_static_name_reuse_before_adapter_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
    mismatch: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, current_run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
            "opaque": "SENSITIVE_REPLACEMENT_VALUE",
        },
    )
    expected_key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    if mismatch == "backend":
        expected_key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ)
    elif mismatch == "runtime-kind":
        expected_key = DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)
    expected = ExpectedRunIdentity(
        "demo",
        str(uuid.uuid4()) if mismatch == "run-id" else current_run_id,
        expected_key,
    )
    before = (rpaths.owner.read_bytes(), rpaths.state.read_bytes())
    effects: list[str] = []
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        target_name,
        lambda *_args, **_kwargs: effects.append("cloud"),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        target_name,
        lambda *_args, **_kwargs: effects.append("lima"),
    )

    with pytest.raises(StateError, match="run identity changed before lifecycle operation") as captured:
        dispatch("demo", roots=roots, expected_identity=expected, **kwargs)

    assert (current_run_id, DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)) != (
        expected.run_id,
        expected.dispatch_key,
    )
    assert effects == []
    assert (rpaths.owner.read_bytes(), rpaths.state.read_bytes()) == before
    assert "SENSITIVE_REPLACEMENT_VALUE" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def _install_adapter_side_effect_spies(
    monkeypatch: pytest.MonkeyPatch,
    effects: list[str],
) -> None:
    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"adapter side effect reached: {effect}")

    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name == "console.log":
            forbidden("console-read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(state, "locked", lambda *_a, **_k: forbidden("lifecycle-lock"))
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "_get_conn", lambda *_a, **_k: forbidden("libvirt"))
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "_write_state", lambda *_a, **_k: forbidden("cloud-write"))
    monkeypatch.setattr(runtime_dispatch.lima, "_run_command", lambda *_a, **_k: forbidden("limactl"))
    monkeypatch.setattr(runtime_dispatch.lima, "_write_state", lambda *_a, **_k: forbidden("lima-write"))
    monkeypatch.setattr(
        runtime_dispatch.lima,
        "_instance_info_or_none",
        lambda *_a, **_k: forbidden("lima-inspect"),
    )
    monkeypatch.setattr(Path, "read_text", guarded_read_text)


@pytest.mark.parametrize(
    ("runtime_kind", "backend", "adapter_name"),
    [
        ("cloud-image", "kvm", "cloud_runtime"),
        ("cloud-image", "libvirt-hvf", "cloud_runtime"),
        ("cloud-image", "lima-vz", "lima"),
    ],
)
@pytest.mark.parametrize(("_operation", "target_name", "dispatch", "kwargs"), _OPERATIONS)
def test_existing_operations_route_by_the_durable_dispatch_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
    backend: str,
    adapter_name: str,
    _operation: RuntimeOperation,
    target_name: str,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": runtime_kind,
            "backend": backend,
            "name": "demo",
            "status": "stopped",
        },
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def selected(name: str, **call_kwargs: Any) -> object:
        calls.append((adapter_name, name, call_kwargs))
        if target_name == "logs":
            return iter(("line\n",))
        if target_name == "inspect_run":
            return object()
        expected = call_kwargs["_expected_record"]
        expected_snapshot = call_kwargs["_expected_snapshot"]
        assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
        with state.locked_existing_run(roots, name, expected=expected, expected_snapshot=expected_snapshot) as mutation:
            terminal = {
                "start": "running",
                "stop": "stopped",
                "rm": "removed",
            }[target_name]
            current = mutation.mutable_state()
            initial = mutation.initial_snapshot
            written = (
                current
                if initial.state["status"] == terminal and target_name != "rm"
                else mutation.write_state(terminal, current)
            )
            outcome = _issue_lifecycle_adapter_outcome(
                mutation.record,
                initial.state["status"],
                state.lifecycle_revision(initial),
                terminal,
                state.lifecycle_revision(written),
            )
            if target_name == "rm" and call_kwargs.get("volumes") is True:
                mutation.delete_run_tree()
            return outcome

    monkeypatch.setattr(getattr(runtime_dispatch, adapter_name), target_name, selected)
    result = dispatch("demo", roots=roots, **kwargs)

    assert result is not None
    if target_name == "inspect_run":
        assert isinstance(result, InspectRecord)
        assert calls == []
        return
    if target_name == "logs":
        assert calls == []
        assert list(result) == ["line\n"]
    assert len(calls) == 1
    called_adapter, called_name, call_kwargs = calls[0]
    assert (called_adapter, called_name) == (adapter_name, "demo")
    expected_record = call_kwargs.pop("_expected_record")
    expected_snapshot = call_kwargs.pop("_expected_snapshot", None)
    assert isinstance(expected_record, ExistingRunRecord)
    assert expected_record.dispatch_key == DispatchKey(RuntimeKind(runtime_kind), RuntimeBackend(backend))
    if target_name in {"start", "stop", "rm"}:
        assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
    assert call_kwargs == {"roots": roots, **kwargs}


@pytest.mark.parametrize("schema_version", [1, 2])
def test_inspect_is_one_snapshot_typed_immutable_allowlisted_and_state_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int,
) -> None:
    roots = _roots(tmp_path)
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "runtime_kind": "cloud-image",
        "backend": "kvm",
        "status": "running",
        "lifecycle_revision": 7,
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T00:01:00Z",
        "base": {
            "digest": "sha256:" + "a" * 64,
            "arch": "x86_64",
            "disk_format": "qcow2",
            "local_path": "/private/SENSITIVE_VALUE/base.qcow2",
        },
        "layers": [
            {
                "digest": "sha256:" + "b" * 64,
                "target_dev": "vdb",
                "local_path": "/private/SENSITIVE_VALUE/layer.squashfs",
                "serial": "internal-layer-id",
            }
        ],
        "memory_mib": 2048,
        "vcpus": 2,
        "network": "default",
        "ports": [{"host_ip": "127.0.0.1", "host_port": 8080, "guest_port": 80, "protocol": "tcp"}],
        "volumes": [
            {
                "name": "data",
                "mount_path": "/srv/data",
                "filesystem": "ext4",
                "read_only": False,
                "target_dev": "vdc",
                "host_path": "/private/SENSITIVE_VALUE/data.raw",
                "backend_name": "internal-volume-id",
            }
        ],
        "ssh": {"host": "127.0.0.1", "port": 2222, "config": "/private/SENSITIVE_VALUE/ssh"},
        "guest_ip": "192.0.2.10",
        "domain_uuid": "SENSITIVE_VALUE",
        "environment": {"API_KEY": "SENSITIVE_VALUE"},
        "cleanup_flags": {"SENSITIVE_VALUE": True},
        "error": "SENSITIVE_VALUE",
    }
    _write_ledger(roots, record=payload)
    before = _snapshot_tree(roots.state)
    original_reader = state.read_run_ledger_snapshot
    reads = 0

    def counted_reader(read_roots: state.StatePaths, name: str) -> state.RunLedgerSnapshot:
        nonlocal reads
        reads += 1
        return original_reader(read_roots, name)

    monkeypatch.setattr(state, "read_run_ledger_snapshot", counted_reader)
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "inspect_run", lambda *_a, **_k: pytest.fail("adapter"))
    monkeypatch.setattr(runtime_dispatch.lima, "inspect_run", lambda *_a, **_k: pytest.fail("adapter"))
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "reconcile_run", lambda *_a, **_k: pytest.fail("reconcile"))
    monkeypatch.setattr(runtime_dispatch.lima, "reconcile_run", lambda *_a, **_k: pytest.fail("reconcile"))
    monkeypatch.setattr(state, "locked_existing_run", lambda *_a, **_k: pytest.fail("lock"))

    inspected = runtime_dispatch.inspect_run("demo", roots=roots)

    assert isinstance(inspected, InspectRecord)
    assert inspected.schema_version == 1
    assert inspected.record.state_schema_version == schema_version
    assert inspected.lifecycle.status == "running"
    assert inspected.lifecycle.lifecycle_revision == 7
    assert inspected.detail.base.disk_format == "qcow2"
    assert inspected.detail.layers[0].target_dev == "vdb"
    assert inspected.detail.ports[0].host_port == 8080
    assert inspected.detail.volumes[0].mount_path == "/srv/data"
    assert reads == 1
    assert _snapshot_tree(roots.state) == before
    rendered = repr(inspected)
    assert "SENSITIVE_VALUE" not in rendered
    assert "local_path" not in rendered
    assert "host_path" not in rendered
    assert "backend_name" not in rendered
    with pytest.raises(FrozenInstanceError):
        inspected.lifecycle.status = "stopped"  # type: ignore[misc]


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
@pytest.mark.parametrize(
    ("_operation", "target_name", "dispatch", "kwargs"),
    [entry for entry in _OPERATIONS if entry[0] not in {RuntimeOperation.INSPECT, RuntimeOperation.LOGS}],
)
def test_eager_dispatch_rejects_identity_and_kind_swap_between_selection_and_adapter_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    _operation: RuntimeOperation,
    target_name: str,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "stopped",
        },
    )
    calls: list[str] = []
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, target_name, lambda *_a, **_k: calls.append("cloud"))
    monkeypatch.setattr(runtime_dispatch.lima, target_name, lambda *_a, **_k: calls.append("lima"))
    original_reader = state.read_run_dispatch_record
    reads = 0

    def racing_reader(read_roots: state.StatePaths, name: str) -> ExistingRunRecord:
        nonlocal reads
        record = original_reader(read_roots, name)
        reads += 1
        if reads == 1:
            replacement_id = str(uuid.uuid4())
            rpaths.owner.write_text(
                json.dumps({"schema_version": 1, "run_id": replacement_id, "name": "demo"}) + "\n",
                encoding="utf-8",
            )
            rpaths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "runtime_kind": "oci-root",
                        "backend": "kvm",
                        "name": "demo",
                        "run_id": replacement_id,
                        "status": "stopped",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        return record

    monkeypatch.setattr(state, "read_run_dispatch_record", racing_reader)
    with pytest.raises(StateError, match="run ledger changed during dispatch"):
        dispatch("demo", roots=roots, **kwargs)
    assert reads == 2
    assert calls == []


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
def test_log_stream_revalidates_bound_record_before_calling_or_entering_adapter_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "stopped",
        },
    )
    rpaths.root.chmod(0o700)
    rpaths.console.write_bytes(b"owned bytes")
    rpaths.console.chmod(0o600)
    calls: list[str] = []

    def forbidden_logs(*_args: Any, **_kwargs: Any):
        calls.append("called")
        yield "must-not-be-observed"

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "logs", forbidden_logs)
    monkeypatch.setattr(runtime_dispatch.lima, "logs", forbidden_logs)
    stream = runtime_dispatch.logs("demo", roots=roots)
    assert calls == []

    replacement_id = str(uuid.uuid4())
    rpaths.owner.write_text(
        json.dumps({"schema_version": 1, "run_id": replacement_id, "name": "demo"}) + "\n",
        encoding="utf-8",
    )
    rpaths.state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_kind": "oci-root",
                "backend": "kvm",
                "name": "demo",
                "run_id": replacement_id,
                "status": "stopped",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events = list(stream.events())
    assert isinstance(events[-1], LogTerminalEvent)
    assert events[-1].outcome.category is LogTerminalCategory.ERROR
    assert events[-1].outcome.error_category is LogErrorCategory.RUN_CHANGED
    assert calls == []


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
@pytest.mark.parametrize(
    ("_operation", "dispatch", "kwargs"),
    [entry for entry in _ADAPTER_ENTRY_OPERATIONS if entry[0] is not RuntimeOperation.INSPECT],
)
def test_adapter_entry_guard_blocks_swap_after_dispatch_revalidation_before_real_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    _operation: RuntimeOperation,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "stopped",
        },
    )
    rpaths.console.write_text("old console\n", encoding="utf-8")
    effects: list[str] = []
    _install_adapter_side_effect_spies(monkeypatch, effects)
    original_reader = state.read_run_dispatch_record
    reads = 0

    def racing_reader(read_roots: state.StatePaths, name: str) -> ExistingRunRecord:
        nonlocal reads
        record = original_reader(read_roots, name)
        reads += 1
        if reads == 2:
            _replace_ledger_with_oci(roots, rpaths)
        return record

    monkeypatch.setattr(state, "read_run_dispatch_record", racing_reader)

    with pytest.raises(StateError, match="run ledger changed"):
        result = dispatch("demo", roots=roots, **kwargs)
        if _operation is RuntimeOperation.LOGS:
            next(result)

    assert reads == (3 if _operation is RuntimeOperation.LOGS else 2)
    assert effects == []


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
@pytest.mark.parametrize(("_operation", "dispatch", "kwargs"), _ADAPTER_ENTRY_OPERATIONS[:3])
def test_adapter_entry_guard_blocks_run_swap_during_secure_reread_before_real_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    _operation: RuntimeOperation,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "stopped",
        },
    )
    rpaths.console.write_text("old console\n", encoding="utf-8")
    effects: list[str] = []
    _install_adapter_side_effect_spies(monkeypatch, effects)
    original_exact_reader = state._read_exact_private_file
    ledger_reads = 0

    def swapping_exact_reader(directory_fd: int, filename: str, **kwargs: Any) -> bytes:
        nonlocal ledger_reads
        content = original_exact_reader(directory_fd, filename, **kwargs)
        ledger_reads += 1
        if ledger_reads == 1:
            rpaths.root.rename(roots.runs / "old-demo")
            replacement_paths, _ = _write_ledger(
                roots,
                record={
                    "schema_version": 2,
                    "runtime_kind": "oci-root",
                    "backend": "kvm",
                    "status": "stopped",
                },
            )
            replacement_paths.console.write_text("replacement console\n", encoding="utf-8")
        return content

    monkeypatch.setattr(state, "_read_exact_private_file", swapping_exact_reader)

    with pytest.raises(StateError, match="existing run changed during lifecycle mutation"):
        result = dispatch("demo", roots=roots, **kwargs)
        if _operation is RuntimeOperation.LOGS:
            next(result)

    assert ledger_reads == 2
    assert effects == []


@pytest.mark.parametrize(("operation", "_target_name", "dispatch", "kwargs"), _OPERATIONS)
def test_oci_root_dispatch_returns_typed_capability_error_before_adapter_or_file_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: RuntimeOperation,
    _target_name: str,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "oci-root",
            "backend": "kvm",
            "name": "demo",
            "status": "stopped",
        },
    )
    before = _snapshot_tree(roots.state)
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        operation.value if operation is not RuntimeOperation.INSPECT else "inspect_run",
        lambda *_a, **_k: pytest.fail("cloud adapter called"),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        operation.value if operation is not RuntimeOperation.INSPECT else "inspect_run",
        lambda *_a, **_k: pytest.fail("Lima adapter called"),
    )

    with pytest.raises(RuntimeCapabilityError) as captured:
        dispatch("demo", roots=roots, **kwargs)

    error = captured.value
    assert error.code == "runtime-operation-unavailable"
    assert error.operation is operation
    assert error.dispatch_key == DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)
    assert _snapshot_tree(roots.state) == before


def test_ps_securely_projects_mixed_durable_runs_without_adapters_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    common = {
        "base": {
            "digest": "sha256:" + "a" * 64,
            "arch": "x86_64",
            "local_path": "/private/SENSITIVE_VALUE/base.qcow2",
        },
        "layers": [
            {
                "digest": "sha256:" + "b" * 64,
                "target_dev": "vdb",
                "local_path": "/private/SENSITIVE_VALUE/layer.squashfs",
            }
        ],
        "volumes": [
            {
                "name": "data",
                "mount_path": "/srv/Data",
                "filesystem": "ext4",
                "read_only": False,
                "target_dev": "vdc",
                "host_path": "/private/SENSITIVE_VALUE/volume.raw",
                "backend_name": "internal-volume-name",
            }
        ],
        "ports": [{"host_ip": "127.0.0.1", "host_port": 8080, "guest_port": 80, "protocol": "tcp"}],
        "memory_mib": 2048,
        "vcpus": 2,
        "network": "default",
        "ssh": {"host": "127.0.0.1", "port": 2222},
        "guest_ip": "192.0.2.10",
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T00:01:00Z",
        "ssh_config_file": "/private/SENSITIVE_VALUE/ssh.config",
    }
    for name, backend in (("a-kvm", "kvm"), ("b-hvf", "libvirt-hvf"), ("c-lima", "lima-vz")):
        _write_ledger(
            roots,
            name=name,
            record={
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": backend,
                "status": "stopped",
                **common,
            },
        )
    _write_ledger(
        roots,
        name="d-oci",
        record={
            "schema_version": 2,
            "runtime_kind": "oci-root",
            "backend": "kvm",
            "status": "root-mounted",
            "created_at": "2026-08-28T00:00:00Z",
        },
    )
    broken = roots.runs / "broken"
    broken.mkdir()
    (broken / "owner.json").write_text("{}\n", encoding="utf-8")
    (broken / "state.json").write_text("{SENSITIVE_VALUE\n", encoding="utf-8")
    invalid_name = "BAD NAME"
    invalid = roots.runs / invalid_name
    invalid.mkdir()
    (invalid / "owner.json").write_text("SENSITIVE_VALUE", encoding="utf-8")

    before = _snapshot_tree(roots.state)
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "reconcile_run", lambda *_a, **_k: pytest.fail("adapter"))
    monkeypatch.setattr(runtime_dispatch.lima, "reconcile_run", lambda *_a, **_k: pytest.fail("adapter"))

    result = runtime_dispatch.ps(roots=roots)

    assert [summary.name for summary in result.summaries] == ["a-kvm", "b-hvf", "c-lima", "d-oci"]
    assert all(summary.stale is True for summary in result.summaries)
    assert {summary.status for summary in result.summaries} == {"stopped", "root-mounted"}
    expected_detail_keys = {
        "base_digest",
        "base_arch",
        "layers",
        "memory_mib",
        "vcpus",
        "network",
        "ports",
        "volumes",
        "ssh",
        "guest_ip",
        "created_at",
        "updated_at",
    }
    assert all(set(summary.details) == expected_detail_keys for summary in result.summaries)
    rendered = repr(result)
    assert "SENSITIVE_VALUE" not in rendered
    assert "local_path" not in rendered
    assert "host_path" not in rendered
    assert "backend_name" not in rendered
    expected_token = "entry-" + hashlib.sha256(invalid_name.encode()).hexdigest()[:12]
    assert [(error.name, error.entry_token, error.code) for error in result.errors] == [
        ("broken", None, "invalid-ledger"),
        (None, expected_token, "invalid-entry"),
    ]
    assert all(error.operation is RuntimeOperation.PS for error in result.errors)
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    "malicious_fields",
    [
        {"base": {"digest": "SENSITIVE_VALUE", "arch": "x86_64"}},
        {"base": {"digest": "sha256:" + "a" * 64, "arch": "SENSITIVE_VALUE"}},
        {"layers": [{"digest": "SENSITIVE_VALUE"}]},
        {"layers": [{"digest": "sha256:" + "b" * 64, "target_dev": "SENSITIVE_VALUE"}]},
        {"network": "SENSITIVE_VALUE"},
        {"ports": [{"host_ip": "SENSITIVE_VALUE", "host_port": 8080, "guest_port": 80, "protocol": "tcp"}]},
        {"ports": [{"host_ip": "127.0.0.1", "host_port": 8080, "guest_port": 80, "protocol": "SENSITIVE_VALUE"}]},
        {"volumes": [{"name": "SENSITIVE_VALUE"}]},
        {"volumes": [{"name": "data", "mount_path": "SENSITIVE_VALUE"}]},
        {"volumes": [{"name": "data", "filesystem": "SENSITIVE_VALUE"}]},
        {"ssh": {"host": "SENSITIVE_VALUE", "port": 22}},
        {"guest_ip": "SENSITIVE_VALUE"},
        {"created_at": "SENSITIVE_VALUE"},
        {"updated_at": "SENSITIVE_VALUE"},
    ],
)
def test_ps_never_reflects_unvalidated_public_string_fields(
    tmp_path: Path,
    malicious_fields: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
            **malicious_fields,
        },
    )

    result = runtime_dispatch.ps(roots=roots)

    assert result.summaries == ()
    assert len(result.errors) == 1
    assert result.errors[0].code == "invalid-ledger"
    assert "SENSITIVE_VALUE" not in repr(result)


def test_run_summary_public_constructor_rejects_semantically_untrusted_details() -> None:
    record = ExistingRunRecord(
        "demo",
        "862ffb44-6795-4618-b2d8-c0750439fac3",
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    details = MappingProxyType(
        {
            "base_digest": "SENSITIVE_VALUE",
            "base_arch": "x86_64",
            "layers": (),
            "memory_mib": None,
            "vcpus": None,
            "network": None,
            "ports": (),
            "volumes": (),
            "ssh": MappingProxyType({"host": None, "port": 22}),
            "guest_ip": None,
            "created_at": None,
            "updated_at": None,
        }
    )
    with pytest.raises(ValueError, match="invalid base digest"):
        RunSummary(record, "stopped", details, stale=True)
    with pytest.raises(TypeError, match="immutable detail mapping"):
        RunSummary(record, "stopped", UserDict(details), stale=True)
    nested_mutable = dict(details)
    nested_mutable["base_digest"] = "sha256:" + "a" * 64
    nested_mutable["ssh"] = UserDict({"host": None, "port": 22})
    with pytest.raises(TypeError, match="deeply immutable"):
        RunSummary(record, "stopped", MappingProxyType(nested_mutable), stale=True)


def test_reconcile_routes_each_exact_record_and_keeps_oci_durable_summary_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    for name, kind, backend, status in (
        ("a-kvm", "cloud-image", "kvm", "stopped"),
        ("b-hvf", "cloud-image", "libvirt-hvf", "stopped"),
        ("c-lima", "cloud-image", "lima-vz", "stopped"),
        ("d-oci", "oci-root", "kvm", "fetching"),
    ):
        _write_ledger(
            roots,
            name=name,
            record={
                "schema_version": 2,
                "runtime_kind": kind,
                "backend": backend,
                "status": status,
                "created_at": "2026-08-28T00:00:00Z",
            },
        )
    calls: list[tuple[str, str]] = []

    def cloud_adapter(name, *, _expected_record, **_kwargs):
        calls.append((name, _expected_record.dispatch_key.backend.value))
        return {
            "state": {"status": "running", "guest_ip": "SENSITIVE_VALUE"},
            "warnings": ["SENSITIVE_VALUE"] if name == "a-kvm" else [],
        }

    def lima_adapter(name, *, _expected_record, **_kwargs):
        calls.append((name, _expected_record.dispatch_key.backend.value))
        return {"state": {"status": "running", "guest_ip": "SENSITIVE_VALUE"}}

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "reconcile_run", cloud_adapter)
    monkeypatch.setattr(runtime_dispatch.lima, "reconcile_run", lima_adapter)

    result = runtime_dispatch.reconcile(roots=roots)

    assert calls == [("a-kvm", "kvm"), ("b-hvf", "libvirt-hvf"), ("c-lima", "lima-vz")]
    assert [summary.name for summary in result.summaries] == ["a-kvm", "b-hvf", "c-lima", "d-oci"]
    assert [summary.stale for summary in result.summaries] == [False, False, False, True]
    assert [summary.status for summary in result.summaries] == ["stopped", "stopped", "stopped", "fetching"]
    assert [(error.name, error.code, error.operation) for error in result.errors] == [
        ("a-kvm", "runtime-warning", RuntimeOperation.RECONCILE),
        ("d-oci", "runtime-capability", RuntimeOperation.RECONCILE),
    ]
    assert "SENSITIVE_VALUE" not in repr(result)


def test_reconcile_marks_original_durable_snapshot_stale_when_post_adapter_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    rpaths, original_run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
        },
    )

    def replace_after_adapter(*_args, **_kwargs):
        replacement_run_id = str(uuid.uuid4())
        rpaths.owner.write_text(
            json.dumps({"schema_version": 1, "run_id": replacement_run_id, "name": "demo"}) + "\n",
            encoding="utf-8",
        )
        rpaths.state.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "runtime_kind": "cloud-image",
                    "backend": "kvm",
                    "name": "demo",
                    "run_id": replacement_run_id,
                    "status": "running",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {"state": {"status": "running"}}

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "reconcile_run", replace_after_adapter)

    result = runtime_dispatch.reconcile(roots=roots)

    assert len(result.summaries) == 1
    assert result.summaries[0].run_id == original_run_id
    assert result.summaries[0].status == "stopped"
    assert result.summaries[0].stale is True
    assert [(error.name, error.code) for error in result.errors] == [("demo", "runtime-failure")]


def test_reconcile_emits_stable_warning_after_missing_domain_status_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "running",
        },
    )

    class MissingDomainConnection:
        def lookupByName(self, _name: str):
            raise KeyError("SENSITIVE_VALUE")

    monkeypatch.setattr(runtime_dispatch.cloud_runtime.kvm, "connect", lambda _uri: MissingDomainConnection())

    result = runtime_dispatch.reconcile(roots=roots)

    assert len(result.summaries) == 1
    assert result.summaries[0].status == "stopped"
    assert result.summaries[0].stale is False
    assert [(error.name, error.code, error.message) for error in result.errors] == [
        ("demo", "runtime-warning", "runtime status changed during reconciliation")
    ]
    assert "SENSITIVE_VALUE" not in repr(result)


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
def test_reconcile_projects_observed_legacy_status_without_rewriting_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _run_id = _write_ledger(
        roots,
        record={"backend": backend, "status": "running"},
    )
    before = rpaths.state.read_bytes()
    adapter = runtime_dispatch.cloud_runtime if backend == "kvm" else runtime_dispatch.lima
    monkeypatch.setattr(
        adapter,
        "reconcile_run",
        lambda *_args, **_kwargs: {
            "state": {"backend": backend, "status": "stopped"},
            "warnings": ["observed drift"],
        },
    )

    result = runtime_dispatch.reconcile(roots=roots)

    assert [(summary.name, summary.status, summary.stale) for summary in result.summaries] == [
        ("demo", "stopped", False)
    ]
    assert [(error.name, error.code) for error in result.errors] == [("demo", "runtime-warning")]
    assert rpaths.state.read_bytes() == before


@pytest.mark.parametrize("failure_mode", ["query", "foreign", "write"])
def test_lima_reconcile_failure_is_typed_stale_and_preserves_ledger_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "status": "stopped",
        },
    )
    before = (rpaths.owner.read_bytes(), rpaths.state.read_bytes())
    backend_calls: list[str] = []
    writes: list[str] = []

    def query(_name: str) -> dict[str, Any]:
        backend_calls.append("query")
        if failure_mode == "query":
            raise StateError("SENSITIVE_VALUE")
        marker = "00000000-0000-0000-0000-000000000000" if failure_mode == "foreign" else run_id
        return {
            "name": "demo",
            "status": "Running",
            "config": {"env": {"PALIMPSEST_RUN_ID": marker}},
        }

    def write(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        writes.append("write")
        if failure_mode == "write":
            raise OSError("SENSITIVE_VALUE")
        pytest.fail("unexpected Lima ledger write")

    monkeypatch.setattr(runtime_dispatch.lima, "_instance_info_or_none", query)
    monkeypatch.setattr(state.ExistingRunMutation, "write_state", write)

    result = runtime_dispatch.reconcile(roots=roots)

    assert backend_calls == (["query", "query"] if failure_mode == "write" else ["query"])
    assert writes == (["write"] if failure_mode == "write" else [])
    assert (rpaths.owner.read_bytes(), rpaths.state.read_bytes()) == before
    assert [(summary.name, summary.status, summary.stale) for summary in result.summaries] == [
        ("demo", "stopped", True)
    ]
    assert [(error.name, error.code, error.message) for error in result.errors] == [
        ("demo", "runtime-failure", "runtime reconciliation failed")
    ]
    assert "SENSITIVE_VALUE" not in repr(result)


def test_lima_reconcile_detects_cooperative_swap_during_external_query_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    rpaths, old_run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "status": "stopped",
        },
    )
    replacement_run_id = str(uuid.uuid4())
    replacement_owner = (
        json.dumps({"schema_version": 1, "run_id": replacement_run_id, "name": "demo"}, sort_keys=True) + "\n"
    ).encode()
    replacement_state = (
        json.dumps(
            {
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": "lima-vz",
                "name": "demo",
                "run_id": replacement_run_id,
                "status": "running",
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    backend_calls: list[str] = []

    def swapping_query(_name: str) -> dict[str, Any]:
        backend_calls.append("query")
        rpaths.owner.write_bytes(replacement_owner)
        rpaths.state.write_bytes(replacement_state)
        return {
            "name": "demo",
            "status": "Running",
            "config": {"env": {"PALIMPSEST_RUN_ID": old_run_id}},
        }

    monkeypatch.setattr(runtime_dispatch.lima, "_instance_info_or_none", swapping_query)
    monkeypatch.setattr(
        runtime_dispatch.lima,
        "_write_state",
        lambda *_a, **_k: pytest.fail("replacement ledger was overwritten"),
    )

    result = runtime_dispatch.reconcile(roots=roots)

    assert backend_calls == ["query"]
    assert rpaths.owner.read_bytes() == replacement_owner
    assert rpaths.state.read_bytes() == replacement_state
    assert [(summary.run_id, summary.status, summary.stale) for summary in result.summaries] == [
        (old_run_id, "stopped", True)
    ]
    assert [(error.name, error.code) for error in result.errors] == [("demo", "runtime-failure")]


def test_lima_reconcile_does_not_write_stale_preflight_status_when_fresh_status_matches_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    rpaths, run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "status": "stopped",
        },
    )
    statuses = iter(("Running", "Stopped"))
    backend_calls: list[str] = []

    def changing_query(_name: str) -> dict[str, Any]:
        backend_calls.append("query")
        return {
            "name": "demo",
            "status": next(statuses),
            "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
        }

    monkeypatch.setattr(runtime_dispatch.lima, "_instance_info_or_none", changing_query)

    result = runtime_dispatch.reconcile(roots=roots)

    assert backend_calls == ["query", "query"]
    assert [(summary.name, summary.status, summary.stale) for summary in result.summaries] == [
        ("demo", "stopped", False)
    ]
    assert result.errors == ()
    assert state.read_run_state(rpaths)["status"] == "stopped"


def test_ps_missing_runs_root_is_empty_and_does_not_create_directories(tmp_path: Path) -> None:
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")

    result = runtime_dispatch.ps(roots=roots)

    assert result == RunAggregationResult((), ())
    assert not roots.config.exists()
    assert not roots.state.exists()


def test_ps_rejects_ambiguous_runs_parent_and_reports_child_symlink_without_following(
    tmp_path: Path,
) -> None:
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")
    roots.state.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "owner.json").write_text("SENSITIVE_VALUE", encoding="utf-8")
    roots.runs.symlink_to(external, target_is_directory=True)

    with pytest.raises(StateError, match="cannot securely enumerate run ledgers") as captured:
        runtime_dispatch.ps(roots=roots)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SENSITIVE_VALUE" not in str(captured.value)

    roots.runs.unlink()
    roots.runs.mkdir()
    (roots.runs / "linked-run").symlink_to(external, target_is_directory=True)
    result = runtime_dispatch.ps(roots=roots)
    assert result.summaries == ()
    assert [(error.name, error.code) for error in result.errors] == [("linked-run", "invalid-entry")]
    assert "SENSITIVE_VALUE" not in repr(result)


def test_ps_detects_runs_parent_swap_while_enumerating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(roots, record={"status": "stopped"})
    original_reader = state._read_pinned_run_payloads
    swapped = False

    def swapping_reader(
        runs_fd: int,
        name: str,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal swapped
        payloads = original_reader(runs_fd, name, **kwargs)
        if not swapped:
            swapped = True
            roots.runs.rename(roots.state / "old-runs")
            roots.runs.mkdir()
        return payloads

    monkeypatch.setattr(state, "_read_pinned_run_payloads", swapping_reader)

    with pytest.raises(StateError, match="run ledger changed during read") as captured:
        runtime_dispatch.ps(roots=roots)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_ps_detects_runs_parent_swap_between_stat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(roots, record={"status": "stopped"})
    original_open = state._open_readonly_no_follow
    swapped = False

    def swapping_open(path, **kwargs):
        nonlocal swapped
        if not swapped and Path(path) == roots.runs and kwargs.get("directory"):
            swapped = True
            roots.runs.rename(roots.state / "old-runs")
            roots.runs.mkdir()
        return original_open(path, **kwargs)

    monkeypatch.setattr(state, "_open_readonly_no_follow", swapping_open)

    with pytest.raises(StateError, match="cannot securely enumerate run ledgers"):
        runtime_dispatch.ps(roots=roots)


def test_run_aggregation_result_rejects_unsorted_or_duplicate_errors() -> None:
    one = RunAggregationError(
        "z-run",
        None,
        RuntimeOperation.PS,
        None,
        "invalid-ledger",
        "invalid run ledger",
    )
    two = RunAggregationError(
        "a-run",
        None,
        RuntimeOperation.PS,
        None,
        "invalid-ledger",
        "invalid run ledger",
    )
    with pytest.raises(ValueError, match="deterministically sorted"):
        RunAggregationResult((), (one, two))
    with pytest.raises(ValueError, match="unique"):
        RunAggregationResult((), (one, one))


class _FakeProcessSession:
    capabilities = ProcessCapabilities(False, False, False, True)

    def events(self):
        return iter(())

    def write_stdin(self, _data: bytes) -> None:
        return None

    def close_stdin(self) -> None:
        return None

    def resize(self, _rows: int, _columns: int) -> None:
        return None

    def signal(self, _requested: ProcessSignal) -> None:
        return None

    def wait(self) -> ProcessExit:
        return ProcessExit(0, 0, None, ProcessExitCategory.EXITED)

    def close(self) -> None:
        return None


@pytest.mark.parametrize("backend", [RuntimeBackend.KVM, RuntimeBackend.LIBVIRT_HVF])
def test_commit_dispatch_returns_strict_typed_receipt_for_exact_cloud_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: RuntimeBackend,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend.value,
            "status": "running",
        },
    )
    calls: list[tuple[str, str, ExistingRunRecord]] = []

    def selected(name: str, tag: str, *, roots: state.StatePaths, _expected_record: ExistingRunRecord):
        del roots
        calls.append((name, tag, _expected_record))
        return {
            "tag": tag,
            "digest": "sha256:" + "c" * 64,
            "size_bytes": 4096,
            "parent_digest": "sha256:" + "b" * 64,
            "base_image_digest": "sha256:" + "a" * 64,
            "source": "commit",
        }

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "commit", selected)
    result = runtime_dispatch.commit("demo", "captured", roots=roots)

    assert isinstance(result, CommitResult)
    assert result.record == calls[0][2]
    assert result.record.dispatch_key.backend is backend
    assert result.tag == "captured"
    assert result.digest == "sha256:" + "c" * 64
    assert result.size_bytes == 4096


@pytest.mark.parametrize(
    ("runtime_kind", "backend"),
    [(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ), (RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)],
)
def test_commit_unavailable_routes_fail_before_any_adapter_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: RuntimeKind,
    backend: RuntimeBackend,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": runtime_kind.value,
            "backend": backend.value,
            "status": "running",
        },
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "commit",
        lambda *_args, **_kwargs: pytest.fail("cloud commit adapter reached"),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        "commit",
        lambda *_args, **_kwargs: pytest.fail("Lima commit adapter reached"),
        raising=False,
    )

    with pytest.raises(RuntimeCapabilityError) as captured:
        runtime_dispatch.commit("demo", "captured", roots=roots)
    assert captured.value.operation is RuntimeOperation.COMMIT


def test_commit_missing_scp_fails_typed_preflight_before_adapter_or_guest_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "running",
        },
    )
    effects: list[str] = []

    def check(requirement, **_kwargs):
        if requirement.capability_id == "tool.scp":
            return CapabilityCheck(
                "tool.scp",
                "missing",
                False,
                CapabilityErrorCategory.MISSING,
                "required tool is unavailable",
            )
        return CapabilityCheck(requirement.capability_id, "present", True)

    monkeypatch.setattr(runtime_dispatch.platforms, "_check_capability", check)
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "commit",
        lambda *_args, **_kwargs: effects.append("adapter") or pytest.fail("commit adapter reached"),
    )

    with pytest.raises(RuntimePreflightError) as captured:
        runtime_dispatch.commit("demo", "captured", roots=roots)

    assert captured.value.category is CapabilityErrorCategory.MISSING
    assert captured.value.capability_id == "tool.scp"
    assert effects == []


def test_commit_expected_identity_rejects_replacement_before_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _rpaths, original_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "running",
        },
    )
    expected = ExpectedRunIdentity(
        "demo",
        original_id,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    _replace_ledger_with_oci(roots, state.run_paths(roots, "demo"))
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "commit",
        lambda *_args, **_kwargs: pytest.fail("replacement reached adapter"),
    )

    with pytest.raises(StateError, match="identity changed"):
        runtime_dispatch.commit("demo", "captured", roots=roots, expected_identity=expected)


@pytest.mark.parametrize(
    ("backend", "adapter_name"),
    [("kvm", "cloud_runtime"), ("libvirt-hvf", "cloud_runtime")],
)
@pytest.mark.parametrize(("operation", "target"), [("exec", "exec_session"), ("shell", "shell_session")])
def test_process_operations_route_by_durable_record_without_returning_host_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    adapter_name: str,
    operation: str,
    target: str,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "running",
        },
    )
    session = _FakeProcessSession()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def selected(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        return session

    monkeypatch.setattr(getattr(runtime_dispatch, adapter_name), target, selected)
    if operation == "exec":
        result = runtime_dispatch.exec("demo", ["printf", "%s", "literal value"], roots=roots)
    else:
        result = runtime_dispatch.shell("demo", roots=roots)

    assert result is session
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "demo"
    if operation == "exec":
        assert isinstance(args[1], ExecRequest)
        assert args[1].argv == ("printf", "%s", "literal value")
    assert kwargs["roots"] == roots
    assert kwargs["_expected_record"].dispatch_key.backend is RuntimeBackend(backend)


@pytest.mark.parametrize(
    ("operation", "dispatch"),
    [(RuntimeOperation.EXEC, runtime_dispatch.exec), (RuntimeOperation.SHELL, runtime_dispatch.shell)],
)
def test_lima_process_operations_are_typed_unavailable_before_probe_or_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: RuntimeOperation,
    dispatch: Callable[..., Any],
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "status": "running",
        },
    )
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "evaluate_capability_profile",
        lambda *_args, **_kwargs: pytest.fail("capability probe reached"),
    )
    monkeypatch.setattr(runtime_dispatch.lima, "exec_session", lambda *_a, **_k: pytest.fail("adapter reached"))
    monkeypatch.setattr(runtime_dispatch.lima, "shell_session", lambda *_a, **_k: pytest.fail("adapter reached"))

    with pytest.raises(RuntimeCapabilityError) as captured:
        dispatch("demo", *((["true"],) if operation is RuntimeOperation.EXEC else ()), roots=roots)

    assert captured.value.operation is operation


@pytest.mark.parametrize(
    ("operation", "dispatch"),
    [(RuntimeOperation.EXEC, runtime_dispatch.exec), (RuntimeOperation.SHELL, runtime_dispatch.shell)],
)
def test_oci_process_operations_remain_typed_unavailable_before_adapter_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: RuntimeOperation,
    dispatch: Callable[..., Any],
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "oci-root",
            "backend": "kvm",
            "status": "running",
        },
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "exec_session" if operation is RuntimeOperation.EXEC else "shell_session",
        lambda *_args, **_kwargs: pytest.fail("cloud process adapter reached"),
    )

    with pytest.raises(RuntimeCapabilityError) as captured:
        if operation is RuntimeOperation.EXEC:
            dispatch("demo", ["true"], roots=roots)
        else:
            dispatch("demo", roots=roots)

    assert captured.value.operation is operation
    assert captured.value.dispatch_key == DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)


@pytest.mark.parametrize("backend", ["kvm", "libvirt-hvf", "lima-vz"])
@pytest.mark.parametrize(
    ("operation", "dispatch", "initial", "terminal", "volumes"),
    [
        (RuntimeOperation.START, runtime_dispatch.start, "stopped", "running", False),
        (RuntimeOperation.STOP, runtime_dispatch.stop, "running", "stopped", False),
        (RuntimeOperation.RM, runtime_dispatch.rm, "stopped", "removed", True),
    ],
)
def test_lifecycle_dispatch_returns_bound_typed_revision_receipt_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    operation: RuntimeOperation,
    dispatch: Callable[..., LifecycleResult],
    initial: str,
    terminal: str,
    volumes: bool,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": initial,
            "lifecycle_revision": 7,
        },
    )
    adapter = runtime_dispatch.lima if backend == "lima-vz" else runtime_dispatch.cloud_runtime

    def mutate(name: str, **kwargs: Any) -> _LifecycleAdapterOutcome:
        expected = kwargs["_expected_record"]
        with state.locked_existing_run(roots, name, expected=expected) as mutation:
            written = mutation.write_state(terminal, mutation.mutable_state())
            initial_snapshot = mutation.initial_snapshot
            outcome = _issue_lifecycle_adapter_outcome(
                mutation.record,
                initial_snapshot.state["status"],
                state.lifecycle_revision(initial_snapshot),
                terminal,
                state.lifecycle_revision(written),
            )
            if volumes:
                mutation.delete_run_tree()
            return outcome

    monkeypatch.setattr(adapter, operation.value, mutate)
    result = dispatch("demo", roots=roots, **({"volumes": True} if volumes else {}))

    assert result.operation is operation
    assert (result.previous_status, result.current_status) == (initial, terminal)
    assert result.cursor.revision == 8
    assert result.record.dispatch_key.backend.value == backend
    assert rpaths.root.exists() is (not volumes)


def test_lifecycle_public_types_reject_out_of_domain_cursor_and_raw_operation() -> None:
    record = ExistingRunRecord(
        "demo",
        str(uuid.uuid4()),
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    with pytest.raises(ValueError, match="invalid revision"):
        LifecycleCursor(record, 2**63)
    with pytest.raises(ValueError, match="invalid operation"):
        LifecycleResult(
            record,
            "start",  # type: ignore[arg-type]
            "stopped",
            "running",
            LifecycleCursor(record, 1),
        )
    with pytest.raises(ValueError, match="forced shutdown"):
        LifecycleResult(
            record,
            RuntimeOperation.START,
            "stopped",
            "running",
            LifecycleCursor(record, 1),
            LifecycleWarningCategory.FORCED_SHUTDOWN,
            True,
        )
    with pytest.raises(ValueError, match="backend reconciliation"):
        LifecycleResult(
            record,
            RuntimeOperation.RM,
            "removed",
            "removed",
            LifecycleCursor(record, 1),
            LifecycleWarningCategory.BACKEND_RECONCILED,
        )
    with pytest.raises(ValueError, match="backend reconciliation"):
        LifecycleResult(
            record,
            RuntimeOperation.START,
            "stopped",
            "running",
            LifecycleCursor(record, 1),
            LifecycleWarningCategory.BACKEND_RECONCILED,
        )


def test_lifecycle_noop_keeps_revision_and_malicious_adapter_result_is_non_reflective(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
            "lifecycle_revision": 4,
        },
    )
    record = state.read_run_dispatch_record(roots, "demo")
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "stop",
        lambda *_args, **_kwargs: _issue_lifecycle_adapter_outcome(record, "stopped", 4, "stopped", 4),
    )
    result = runtime_dispatch.stop("demo", roots=roots)
    assert result.cursor.revision == 4

    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        "stop",
        lambda *_args, **_kwargs: {"status": "stopped", "secret": "SENSITIVE_VALUE"},
    )
    with pytest.raises(StateError) as captured:
        runtime_dispatch.stop("demo", roots=roots)
    assert "SENSITIVE_VALUE" not in str(captured.value)
    assert captured.value.__context__ is None


def test_lifecycle_forced_shutdown_warning_is_typed_and_not_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "running",
        },
    )

    def force(name: str, **kwargs: Any) -> _LifecycleAdapterOutcome:
        expected = kwargs["_expected_record"]
        with state.locked_existing_run(roots, name, expected=expected) as mutation:
            written = mutation.write_state("stopped", mutation.mutable_state())
            initial_snapshot = mutation.initial_snapshot
            return _issue_lifecycle_adapter_outcome(
                mutation.record,
                initial_snapshot.state["status"],
                state.lifecycle_revision(initial_snapshot),
                "stopped",
                state.lifecycle_revision(written),
                LifecycleWarningCategory.FORCED_SHUTDOWN,
                True,
            )

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "stop", force)
    result = runtime_dispatch.stop("demo", roots=roots)
    assert result.warning_category is LifecycleWarningCategory.FORCED_SHUTDOWN
    assert result.fallback_used is True
    durable = state.read_run_state(state.run_paths(roots, "demo"))
    assert "warning_category" not in durable
    assert "fallback_used" not in durable


@pytest.mark.parametrize(
    ("operation", "durable_status", "live_before", "live_after"),
    [
        (RuntimeOperation.START, "running", "Stopped", "Running"),
        (RuntimeOperation.STOP, "stopped", "Running", "Stopped"),
    ],
)
def test_lima_backend_recovery_returns_authenticated_reconciled_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: RuntimeOperation,
    durable_status: str,
    live_before: str,
    live_after: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "status": durable_status,
            "lifecycle_revision": 3,
            "layers": [],
            "volumes": [],
        },
    )
    live_status = live_before

    def instance() -> dict[str, Any]:
        return {
            "name": "demo",
            "status": live_status,
            "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
        }

    def command(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal live_status
        assert argv[:2] == ["limactl", operation.value]
        live_status = live_after
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runtime_dispatch.lima, "_instance_info_or_none", lambda _name: instance())
    monkeypatch.setattr(runtime_dispatch.lima, "_instance_info", lambda _name: instance())
    monkeypatch.setattr(runtime_dispatch.lima, "_run_command", command)
    monkeypatch.setattr(runtime_dispatch.lima, "_attach_layers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_dispatch.lima, "_guest_ipv4", lambda _name: "192.0.2.9")

    dispatch = runtime_dispatch.start if operation is RuntimeOperation.START else runtime_dispatch.stop
    result = dispatch("demo", roots=roots)

    assert (result.previous_status, result.current_status) == (durable_status, durable_status)
    assert result.cursor.revision == 5
    assert result.warning_category is LifecycleWarningCategory.BACKEND_RECONCILED
    assert result.fallback_used is False
    durable = state.read_run_state(rpaths)
    assert durable["status"] == durable_status
    assert durable["lifecycle_revision"] == 5
    assert "warning_category" not in durable
    assert "fallback_used" not in durable


def test_backend_reconciled_warning_cannot_authorize_status_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
            "lifecycle_revision": 3,
        },
    )

    def malicious(name: str, **kwargs: Any) -> _LifecycleAdapterOutcome:
        expected = kwargs["_expected_record"]
        snapshot = kwargs["_expected_snapshot"]
        with state.locked_existing_run(
            roots,
            name,
            expected=expected,
            expected_snapshot=snapshot,
        ) as mutation:
            written = mutation.write_state("running", mutation.mutable_state())
            outcome = _issue_lifecycle_adapter_outcome(
                mutation.record,
                "stopped",
                3,
                "stopped",
                state.lifecycle_revision(written),
                LifecycleWarningCategory.BACKEND_RECONCILED,
            )
            object.__setattr__(outcome, "status", "running")
            object.__setattr__(
                outcome,
                "authentication_tag",
                runtime_dispatch._lifecycle_outcome_authentication_tag(
                    mutation.record,
                    "stopped",
                    3,
                    "running",
                    state.lifecycle_revision(written),
                    LifecycleWarningCategory.BACKEND_RECONCILED,
                    False,
                ),
            )
            return outcome

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "start", malicious)

    with pytest.raises(StateError, match="invalid recovery metadata"):
        runtime_dispatch.start("demo", roots=roots)


def test_lifecycle_receipt_poison_is_non_reflective_even_after_durable_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
        },
    )

    class Poison:
        def __str__(self) -> str:
            raise RuntimeError("SENSITIVE_VALUE")

        def __repr__(self) -> str:
            raise RuntimeError("SENSITIVE_VALUE")

        def __eq__(self, _other: object) -> bool:
            raise RuntimeError("SENSITIVE_VALUE")

        def __hash__(self) -> int:
            raise RuntimeError("SENSITIVE_VALUE")

    def tamper(name: str, **kwargs: Any) -> _LifecycleAdapterOutcome:
        expected = kwargs["_expected_record"]
        expected_snapshot = kwargs["_expected_snapshot"]
        with state.locked_existing_run(
            roots,
            name,
            expected=expected,
            expected_snapshot=expected_snapshot,
        ) as mutation:
            initial = mutation.initial_snapshot
            written = mutation.write_state("running", mutation.mutable_state())
            outcome = _issue_lifecycle_adapter_outcome(
                mutation.record,
                initial.state["status"],
                state.lifecycle_revision(initial),
                "running",
                state.lifecycle_revision(written),
            )
            object.__setattr__(outcome, "status", Poison())
            return outcome

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "start", tamper)
    with pytest.raises(StateError) as captured:
        runtime_dispatch.start("demo", roots=roots)
    assert str(captured.value) == "runtime adapter returned an invalid lifecycle receipt"
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("dispatch", "initial", "kwargs"),
    [
        (runtime_dispatch.start, "running", {}),
        (runtime_dispatch.stop, "stopped", {}),
        (runtime_dispatch.rm, "removed", {}),
        (runtime_dispatch.rm, "running", {"volumes": True}),
    ],
)
def test_lifecycle_revision_max_rejects_every_operation_before_preflight_or_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispatch: Callable[..., LifecycleResult],
    initial: str,
    kwargs: dict[str, object],
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": initial,
            "lifecycle_revision": 2**63 - 1,
        },
    )
    effects: list[str] = []
    monkeypatch.setattr(
        runtime_dispatch,
        "preflight_existing_record",
        lambda *_args, **_kwargs: effects.append("preflight") or pytest.fail("preflight reached"),
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        dispatch.__name__,
        lambda *_args, **_kwargs: effects.append("adapter") or pytest.fail("adapter reached"),
    )

    with pytest.raises(StateError, match="cannot be incremented"):
        dispatch("demo", roots=roots, **kwargs)
    assert effects == []
