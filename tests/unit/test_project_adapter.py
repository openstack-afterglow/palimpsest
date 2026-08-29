"""Runtime adapter contracts for declarative projects."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import project_adapter, state
from palimpsest_local.errors import ArtifactValidationError, LifecycleError, StateError
from palimpsest_local.project import Project, load_project
from palimpsest_local.project_runtime import (
    PreparedService,
    ProjectLifecycleError,
    ProjectPrepareError,
    ServicePlan,
    down_project,
    project_config_digest,
    project_logs,
    service_config_digest,
    service_run_name,
    stop_project_services,
    up_project,
)
from palimpsest_local.refs import ImageRef, StackRef
from palimpsest_local.runtime_types import (
    CapabilityCheck,
    CloudInitSnapshot,
    DispatchKey,
    ExistingRunRecord,
    ExpectedRunIdentity,
    PreflightReport,
    ResolvedRunRequest,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RunVolumeIntent,
    _issue_lifecycle_adapter_outcome,
    _LifecycleAdapterOutcome,
    run_request_subject_digest,
)


@pytest.fixture(autouse=True)
def _stub_operation_capability_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "test-present", True),
    )


def _roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})


def _stack(tmp_path: Path, marker: bytes = b"base", *, arch: str = "x86_64") -> StackRef:
    image = tmp_path / f"{hashlib.sha256(marker).hexdigest()}.img"
    image.write_bytes(marker)
    digest = f"sha256:{hashlib.sha256(marker).hexdigest()}"
    return StackRef(ImageRef(digest, "raw", arch, "ubuntu", image), ())


def _project(tmp_path: Path, body: str, environment: dict[str, str] | None = None):
    path = tmp_path / "palimpsest.yml"
    path.write_text(body, encoding="utf-8")
    return load_project(path, environment or {})


def _write_run_ledger(
    roots: state.StatePaths,
    name: str,
    *,
    backend: str,
    runtime_kind: str = "cloud-image",
    status: str = "stopped",
) -> state.RunPaths:
    rpaths = state.run_paths(roots, name)
    rpaths.root.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    state.atomic_write_json(
        rpaths.owner,
        {"schema_version": 1, "run_id": run_id, "name": name},
    )
    state.atomic_write_json(
        rpaths.state,
        {
            "schema_version": 2,
            "runtime_kind": runtime_kind,
            "backend": backend,
            "name": name,
            "run_id": run_id,
            "status": status,
        },
    )
    return rpaths


def _write_project_service_ledger(
    project: Project,
    roots: state.StatePaths,
    *,
    run_name: str,
    run_id: str,
) -> Path:
    project_name = project.name
    ppaths = state.project_paths(roots, project_name)
    ppaths.root.mkdir(parents=True, exist_ok=True)
    now = state.utc_now_iso()
    state.atomic_write_json(
        ppaths.state,
        {
            "schema_version": 2,
            "project": project_name,
            "config_digest": project_config_digest(project),
            "services": [
                {
                    "service": "api",
                    "run_name": run_name,
                    "config_digest": "sha256:" + "b" * 64,
                    "run_id": run_id,
                    "backend": "kvm",
                }
            ],
            "order": ["api"],
            "volumes": [],
            "created_at": now,
            "updated_at": now,
        },
    )
    return ppaths.state


def test_desired_digest_binds_resolved_environment_and_runtime_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    image_digest = "sha256:" + "a" * 64
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: {image_digest}
    environment:
      APP_MODE: ${{APP_MODE:?required}}
""",
        {"APP_MODE": "one"},
    )
    stacks = [_stack(tmp_path, b"one"), _stack(tmp_path, b"two")]

    one = project_adapter.build_project_callbacks(
        project,
        _roots(tmp_path),
        lambda _service: stacks[0],
        environment={"APP_MODE": "one"},
    )
    two_env = project_adapter.build_project_callbacks(
        project,
        _roots(tmp_path),
        lambda _service: stacks[0],
        environment={"APP_MODE": "two"},
    )
    two_stack = project_adapter.build_project_callbacks(
        project,
        _roots(tmp_path),
        lambda _service: stacks[1],
        environment={"APP_MODE": "one"},
    )

    assert one.desired_digest is not None
    assert two_env.desired_digest is not None
    assert two_stack.desired_digest is not None
    digest = one.desired_digest(project, project.services["api"])
    assert digest != two_env.desired_digest(project, project.services["api"])
    assert digest != two_stack.desired_digest(project, project.services["api"])
    resolved = one.resolve(project, project.services["api"], "demo-api-1")
    assert "one" not in repr(resolved)


def test_desired_digest_binds_env_file_bytes_without_repr_leak(tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text("APP_VALUE=top-secret-value\n", encoding="utf-8")
    body = f"""services:
  api:
    image: sha256:{"a" * 64}
    env_file: [service.env]
"""
    first_project = _project(tmp_path, body)
    first = project_adapter.build_project_callbacks(
        first_project,
        _roots(tmp_path),
        lambda _service: _stack(tmp_path),
    )
    assert first.desired_digest is not None
    first_digest = first.desired_digest(first_project, first_project.services["api"])

    env_file.write_text("# byte-only change\nAPP_VALUE=top-secret-value\n", encoding="utf-8")
    changed_project = _project(tmp_path, body)
    changed = project_adapter.build_project_callbacks(
        changed_project,
        _roots(tmp_path),
        lambda _service: _stack(tmp_path),
    )
    assert changed.desired_digest is not None
    changed_digest = changed.desired_digest(changed_project, changed_project.services["api"])

    assert first_digest != changed_digest
    assert "top-secret-value" not in repr(first)
    assert "top-secret-value" not in repr(changed)


def test_desired_digest_rejects_cloud_init_file_changed_after_load(tmp_path: Path) -> None:
    cloud_init = tmp_path / "cloud-init.yml"
    cloud_init.write_text("packages: [curl]\n", encoding="utf-8")
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
    cloud_init:
      file: cloud-init.yml
""",
    )
    callbacks = project_adapter.build_project_callbacks(
        project,
        _roots(tmp_path),
        lambda _service: _stack(tmp_path),
    )
    cloud_init.write_text("packages: [git]\n", encoding="utf-8")

    assert callbacks.desired_digest is not None
    with pytest.raises(StateError, match="cloud_init file changed after project validation"):
        callbacks.desired_digest(project, project.services["api"])


def test_kvm_port_forwarding_fails_during_project_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
    ports: ["18080:8080"]
""",
    )
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
    monkeypatch.setattr(project_adapter, "preflight_kvm_volume_support", lambda: None)
    backend_preflights: list[str] = []
    monkeypatch.setattr(
        project_adapter.runtime_dispatch,
        "preflight_run_request",
        lambda request: backend_preflights.append(request.dispatch_key.backend.value),
    )
    callbacks = project_adapter.build_project_callbacks(project, _roots(tmp_path), lambda _service: _stack(tmp_path))
    service = project.services["api"]
    resolved = callbacks.resolve(project, service, "demo-api-1")
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", "demo-api-1", "create", "sha256:" + "b" * 64, None),
        resolved,
    )

    with pytest.raises(ArtifactValidationError, match="per-domain inbound forwarding"):
        callbacks.preflight((prepared,))
    assert backend_preflights == []


def test_compose_custom_network_is_bound_into_exact_run_preflight_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""networks:
  shared:
    external: true
    name: custom-net
services:
  api:
    image: sha256:{"a" * 64}
    networks: [shared]
""",
    )
    roots = _roots(tmp_path)
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    manual_network_checks: list[str] = []
    monkeypatch.setattr(project_adapter.kvm, "validate_network", manual_network_checks.append)
    monkeypatch.setattr(project_adapter.kvm, "validate_domain_name_available", lambda _name: None)
    original_preflight = project_adapter.runtime_dispatch.preflight_run_request
    reports: list[PreflightReport] = []

    def capture_preflight(request: ResolvedRunRequest):
        report = original_preflight(request)
        reports.append(report)
        return report

    monkeypatch.setattr(project_adapter.runtime_dispatch, "preflight_run_request", capture_preflight)
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    service = project.services["api"]
    resolved = callbacks.resolve(project, service, "demo-api-1")
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", "demo-api-1", "create", "sha256:" + "b" * 64, None),
        resolved,
    )

    callbacks.preflight((prepared,))

    assert resolved.request.spec.network == "custom-net"
    assert manual_network_checks == ["custom-net"]
    assert len(reports) == 1
    report = reports[0]
    network_requirement = next(item for item in report.profile.requirements if item.capability_id == "network.libvirt")
    assert network_requirement.selector == "custom-net"


@pytest.mark.parametrize(
    ("backend", "arch"),
    [("kvm", "x86_64"), ("lima-vz", "aarch64")],
)
@pytest.mark.parametrize("with_volume", [False, True])
def test_project_create_preserves_signed_request_and_cloud_init_snapshot_through_start(
    backend: str,
    arch: str,
    with_volume: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud_init = tmp_path / "cloud-init.yml"
    cloud_init.write_text("packages: [curl]\n", encoding="utf-8")
    volume_section = "volumes:\n  data:\n    size: 1GiB\n" if with_volume else ""
    service_volume = "    volumes: [data:/srv/data]\n" if with_volume else ""
    project = _project(
        tmp_path,
        f"""{volume_section}services:
  api:
    image: sha256:{"a" * 64}
    cloud_init:
      file: cloud-init.yml
{service_volume}""",
    )
    roots = _roots(tmp_path)
    service = project.services["api"]
    run_name = service_run_name(project, "api")
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.platforms,
        "select_backend",
        lambda *_args, **_kwargs: backend,
    )
    if backend == "kvm":
        monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
        monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
        monkeypatch.setattr(project_adapter.kvm, "validate_domain_name_available", lambda _name: None)
        if with_volume:
            volume_path = tmp_path / "managed.raw"
            monkeypatch.setattr(project_adapter, "kvm_volume_path", lambda *_args: volume_path)
            monkeypatch.setattr(project_adapter, "preflight_kvm_volume_support", lambda: None)

            def ensure_kvm(*_args: object, **_kwargs: object) -> SimpleNamespace:
                volume_path.write_bytes(b"managed")
                return SimpleNamespace(path=volume_path)

            monkeypatch.setattr(project_adapter, "ensure_kvm_volume", ensure_kvm)
    else:
        monkeypatch.setattr(project_adapter.lima, "available", lambda: True)
        monkeypatch.setattr(project_adapter.lima, "validate_network", lambda _name: None)
        monkeypatch.setattr(project_adapter.lima, "validate_run_spec", lambda _spec: None)
        if with_volume:
            backend_name = project_adapter.lima_backend_name(project.name, "data")
            monkeypatch.setattr(project_adapter, "verify_lima_volume", lambda *_args, **_kwargs: None)
            monkeypatch.setattr(
                project_adapter,
                "ensure_lima_volume",
                lambda *_args, **_kwargs: SimpleNamespace(backend_name=backend_name, created=True),
            )

    received: list[tuple[ResolvedRunRequest, PreflightReport]] = []

    def consume_at_dispatch(
        request: ResolvedRunRequest,
        *,
        preflight: PreflightReport,
        **_kwargs: object,
    ) -> dict[str, str]:
        assert preflight.subject_digest == run_request_subject_digest(request)
        project_adapter.runtime_dispatch.require_run_preflight(request, preflight)
        received.append((request, preflight))
        return {"status": "running"}

    monkeypatch.setattr(project_adapter.runtime_dispatch, "run", consume_at_dispatch)
    callbacks = project_adapter.build_project_callbacks(
        project,
        roots,
        lambda _service: _stack(tmp_path, arch=arch),
    )
    resolved = callbacks.resolve(project, service, run_name)
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", run_name, "create", "sha256:" + "b" * 64, None),
        resolved,
    )

    callbacks.preflight((prepared,))
    callbacks.prepare((prepared,))
    callbacks.start(prepared)

    request, _report = received[0]
    assert isinstance(request.spec.cloud_init, CloudInitSnapshot)
    assert request.spec.cloud_init.packages == ("curl",)
    assert request.spec.cloud_init is resolved.request.spec.cloud_init
    if with_volume:
        assert request is not resolved.request
        assert request.attachments_bound is True
        assert len(request.spec.volumes) == 1
    else:
        assert request is resolved.request
        assert request.spec.volumes == ()


def test_new_kvm_service_receives_project_block_volume_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""volumes:
  data:
    size: 1GiB
services:
  api:
    image: sha256:{"a" * 64}
    volumes: ["data:/var/lib/data"]
    environment:
      APP_MODE: development
""",
    )
    roots = _roots(tmp_path)
    volume_path = tmp_path / "data.raw"
    events: list[str] = []
    original_preflight = project_adapter.runtime_dispatch.preflight_run_request
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    monkeypatch.setattr(
        project_adapter.runtime_dispatch,
        "preflight_run_request",
        lambda request: (events.append("preflight"), original_preflight(request))[1],
    )
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
    monkeypatch.setattr(project_adapter.kvm, "validate_domain_name_available", lambda _name: None)
    monkeypatch.setattr(project_adapter, "kvm_volume_path", lambda *_args: volume_path)
    monkeypatch.setattr(project_adapter, "preflight_kvm_volume_support", lambda: None)

    def ensure_volume(*_args: object, **_kwargs: object) -> SimpleNamespace:
        events.append("prepare")
        volume_path.write_bytes(b"raw-volume")
        return SimpleNamespace(path=volume_path)

    monkeypatch.setattr(
        project_adapter,
        "ensure_kvm_volume",
        ensure_volume,
    )
    received: list[ResolvedRunRequest] = []

    def run(request: ResolvedRunRequest, **_kwargs: object) -> dict[str, str]:
        events.append("run")
        received.append(request)
        return {"status": "running"}

    monkeypatch.setattr(
        project_adapter.runtime_dispatch,
        "run",
        run,
    )
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    service = project.services["api"]
    resolved = callbacks.resolve(project, service, "demo-api-1")
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", "demo-api-1", "create", "sha256:" + "b" * 64, None),
        resolved,
    )

    callbacks.preflight((prepared,))
    callbacks.prepare((prepared,))
    callbacks.start(prepared)

    request = received[0]
    assert events == ["preflight", "prepare", "run"]
    assert request.attachments_bound is True
    assert request.dispatch_key is resolved.request.dispatch_key
    spec = request.spec
    assert spec.environment == (("APP_MODE", "development"),)
    assert spec.volumes[0].host_path == volume_path
    assert spec.volumes[0].mount_path == "/var/lib/data"


def test_stopped_service_restarts_its_existing_backend_not_new_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    _write_run_ledger(roots, "demo-api-1", backend="lima-vz")
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    monkeypatch.setattr(
        project_adapter.lima,
        "is_lima_run",
        lambda _paths: pytest.fail("project callback used the legacy Lima heuristic"),
    )
    calls: list[str] = []

    def start_existing(name: str, **kwargs: object) -> _LifecycleAdapterOutcome:
        calls.append(name)
        expected = kwargs["_expected_record"]
        assert isinstance(expected, ExistingRunRecord)
        expected_snapshot = kwargs["_expected_snapshot"]
        assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
        with state.locked_existing_run(roots, name, expected=expected, expected_snapshot=expected_snapshot) as mutation:
            written = mutation.write_state("running", mutation.mutable_state())
            initial = mutation.initial_snapshot
            return _issue_lifecycle_adapter_outcome(
                mutation.record,
                initial.state["status"],
                state.lifecycle_revision(initial),
                "running",
                state.lifecycle_revision(written),
            )

    monkeypatch.setattr(
        project_adapter.runtime_dispatch.lima,
        "start",
        start_existing,
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.cloud_runtime,
        "start",
        lambda *_args, **_kwargs: pytest.fail("wrong backend"),
    )
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    service = project.services["api"]
    resolved = callbacks.resolve(project, service, "demo-api-1")
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", "demo-api-1", "start", "sha256:" + "b" * 64, "stopped"),
        resolved,
    )

    callbacks.start(prepared)

    assert calls == ["demo-api-1"]


@pytest.mark.parametrize(
    ("backend", "adapter_name", "expected_backend"),
    [
        ("kvm", "cloud_runtime", RuntimeBackend.KVM),
        ("libvirt-hvf", "cloud_runtime", RuntimeBackend.LIBVIRT_HVF),
        ("lima-vz", "lima", RuntimeBackend.LIMA_VZ),
    ],
)
@pytest.mark.parametrize("operation", ["inspect", "start", "stop", "remove", "logs"])
def test_existing_project_callbacks_route_from_durable_run_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    adapter_name: str,
    expected_backend: RuntimeBackend,
    operation: str,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    _write_run_ledger(roots, run_name, backend=backend)
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    targets = {
        "inspect": "inspect_run",
        "start": "start",
        "stop": "stop",
        "remove": "rm",
        "logs": "logs",
    }
    target_name = targets[operation]
    calls: list[tuple[str, dict[str, object]]] = []

    def selected(name: str, **kwargs: object) -> object:
        calls.append((name, kwargs))
        if operation == "logs":
            return iter(("project log\n",))
        if operation in {"start", "stop", "remove"}:
            expected = kwargs["_expected_record"]
            assert isinstance(expected, ExistingRunRecord)
            expected_snapshot = kwargs["_expected_snapshot"]
            assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
            with state.locked_existing_run(
                roots, name, expected=expected, expected_snapshot=expected_snapshot
            ) as mutation:
                terminal = "running" if operation == "start" else "removed" if operation == "remove" else "stopped"
                initial = mutation.initial_snapshot
                written = (
                    mutation.mutable_state()
                    if initial.state["status"] == terminal and operation != "remove"
                    else mutation.write_state(terminal, mutation.mutable_state())
                )
                outcome = _issue_lifecycle_adapter_outcome(
                    mutation.record,
                    initial.state["status"],
                    state.lifecycle_revision(initial),
                    terminal,
                    state.lifecycle_revision(written),
                )
                if operation == "remove":
                    mutation.delete_run_tree()
                return outcome
        return {"name": name, "status": "stopped"}

    selected_adapter = getattr(project_adapter.runtime_dispatch, adapter_name)
    other_adapter = (
        project_adapter.runtime_dispatch.lima
        if adapter_name == "cloud_runtime"
        else project_adapter.runtime_dispatch.cloud_runtime
    )
    monkeypatch.setattr(selected_adapter, target_name, selected)
    monkeypatch.setattr(
        other_adapter,
        target_name,
        lambda *_args, **_kwargs: pytest.fail("dispatcher selected the wrong project runtime adapter"),
    )
    monkeypatch.setattr(
        project_adapter.lima,
        "is_lima_run",
        lambda *_args: pytest.fail("project callback used the legacy Lima heuristic"),
    )

    if operation == "inspect":
        result = callbacks.inspect(run_name)
    elif operation == "start":
        service = project.services["api"]
        result = callbacks.start(
            PreparedService(
                project,
                service,
                ServicePlan("api", run_name, "start", "sha256:" + "b" * 64, "stopped"),
                None,
            )
        )
    elif operation == "stop":
        result = callbacks.stop(run_name)
    elif operation == "remove":
        result = callbacks.remove(run_name)
    else:
        result = list(callbacks.logs(run_name, False))

    if operation == "logs":
        assert result == ["project log\n"]
    elif operation == "inspect":
        assert result == {"name": run_name, "status": "stopped"}
    else:
        assert result.record.name == run_name
    assert len(calls) == 1
    called_name, kwargs = calls[0]
    assert called_name == run_name
    expected_record = kwargs.pop("_expected_record")
    expected_snapshot = kwargs.pop("_expected_snapshot", None)
    assert isinstance(expected_record, ExistingRunRecord)
    assert expected_record.dispatch_key.backend is expected_backend
    if operation in {"start", "stop", "remove"}:
        assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
    expected_options: dict[str, object] = {"roots": roots}
    if operation == "remove":
        expected_options["volumes"] = True
    elif operation == "logs":
        expected_options["follow"] = False
    assert kwargs == expected_options


@pytest.mark.parametrize("operation", ["start", "stop", "remove", "logs"])
def test_existing_project_callbacks_bind_expected_identity_before_backend_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    rpaths = _write_run_ledger(roots, run_name, backend="kvm")
    state_payload = state.read_json(rpaths.state)
    state_payload["opaque"] = "SENSITIVE_REPLACEMENT_VALUE"
    state.atomic_write_json(rpaths.state, state_payload)
    expected = ExpectedRunIdentity(
        run_name,
        str(uuid.uuid4()),
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    before = (rpaths.owner.read_bytes(), rpaths.state.read_bytes())
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    target_name = {"start": "start", "stop": "stop", "remove": "rm", "logs": "logs"}[operation]
    effects: list[str] = []
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.cloud_runtime,
        target_name,
        lambda *_args, **_kwargs: effects.append("cloud"),
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.lima,
        target_name,
        lambda *_args, **_kwargs: effects.append("lima"),
    )

    def invoke() -> object:
        if operation == "start":
            return callbacks.start(
                PreparedService(
                    project,
                    project.services["api"],
                    ServicePlan("api", run_name, "start", "sha256:" + "b" * 64, "stopped"),
                    None,
                ),
                expected_identity=expected,
            )
        if operation == "stop":
            return callbacks.stop(run_name, expected_identity=expected)
        if operation == "remove":
            return callbacks.remove(run_name, expected_identity=expected)
        return callbacks.logs(run_name, False, expected_identity=expected)

    with pytest.raises(StateError, match="run identity changed before lifecycle operation") as captured:
        invoke()

    assert effects == []
    assert (rpaths.owner.read_bytes(), rpaths.state.read_bytes()) == before
    assert "SENSITIVE_REPLACEMENT_VALUE" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_project_callbacks_preserve_absent_removed_noop_and_foreign_lima_collision_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    monkeypatch.setattr(
        project_adapter.runtime_dispatch,
        "rm",
        lambda *_args, **_kwargs: pytest.fail("absent service removal entered runtime dispatcher"),
    )

    assert callbacks.remove(run_name) == {"name": run_name, "status": "removed"}

    monkeypatch.setattr(project_adapter.lima, "available", lambda: True)
    monkeypatch.setattr(
        project_adapter.lima, "inspect_instance_status", lambda name: "running" if name == run_name else None
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch,
        "inspect_run",
        lambda *_args, **_kwargs: pytest.fail("foreign name probe entered owned-run dispatcher"),
    )
    assert callbacks.inspect(run_name) == {"status": "running"}


@pytest.mark.parametrize(
    ("operation", "status", "swap_on_inspect", "adapter_method"),
    [
        ("stop", "running", 1, "stop"),
        ("remove", "removed", 3, "rm"),
        ("logs", "running", 1, "logs"),
    ],
)
def test_project_operations_reject_cooperative_name_reuse_after_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    status: str,
    swap_on_inspect: int,
    adapter_method: str,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    rpaths = _write_run_ledger(roots, run_name, backend="kvm", status=status)
    old_run_id = state.read_json(rpaths.owner)["run_id"]
    assert isinstance(old_run_id, str)
    project_state = _write_project_service_ledger(project, roots, run_name=run_name, run_id=old_run_id)
    project_before = project_state.read_bytes()
    replacement_run_id = str(uuid.uuid4())
    replacement_owner = {"schema_version": 1, "run_id": replacement_run_id, "name": run_name}
    replacement_state = {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "kvm",
        "name": run_name,
        "run_id": replacement_run_id,
        "status": status,
        "opaque": "SENSITIVE_REPLACEMENT_VALUE",
    }
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    inspections = 0

    def racing_inspect(name: str) -> object:
        nonlocal inspections
        assert name == run_name
        inspections += 1
        if inspections == swap_on_inspect:
            state.atomic_write_json(rpaths.owner, replacement_owner)
            state.atomic_write_json(rpaths.state, replacement_state)
        return {"owner": {"run_id": old_run_id}, "state": {"backend": "kvm", "status": status}}

    callbacks = replace(callbacks, inspect=racing_inspect)
    effects: list[str] = []
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.cloud_runtime,
        adapter_method,
        lambda *_args, **_kwargs: effects.append("cloud"),
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.lima,
        adapter_method,
        lambda *_args, **_kwargs: effects.append("lima"),
    )

    def invoke() -> object:
        if operation == "stop":
            return stop_project_services(project, callbacks, ["api"], roots=roots)
        if operation == "remove":
            return down_project(project, callbacks, roots=roots)
        return list(project_logs(project, callbacks, ["api"], roots=roots))

    with pytest.raises(StateError, match="run identity changed before lifecycle operation") as captured:
        invoke()

    assert effects == []
    assert inspections == swap_on_inspect
    assert project_state.read_bytes() == project_before
    assert state.read_run_dispatch_record(roots, run_name).run_id == replacement_run_id
    assert state.read_json(rpaths.state)["opaque"] == "SENSITIVE_REPLACEMENT_VALUE"
    assert "SENSITIVE_REPLACEMENT_VALUE" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_project_restart_rejects_cooperative_name_reuse_after_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    rpaths = _write_run_ledger(roots, run_name, backend="kvm", status="stopped")
    old_run_id = state.read_json(rpaths.owner)["run_id"]
    assert isinstance(old_run_id, str)
    project_state = _write_project_service_ledger(project, roots, run_name=run_name, run_id=old_run_id)
    project_payload = state.read_json(project_state)
    project_payload["services"][0]["config_digest"] = service_config_digest(project, "api")
    state.atomic_write_json(project_state, project_payload)
    project_before = project_state.read_bytes()
    replacement_run_id = str(uuid.uuid4())
    replacement_state = {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "kvm",
        "name": run_name,
        "run_id": replacement_run_id,
        "status": "stopped",
        "opaque": "SENSITIVE_REPLACEMENT_VALUE",
    }
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    inspections = 0

    def racing_inspect(name: str) -> object:
        nonlocal inspections
        assert name == run_name
        inspections += 1
        if inspections == 2:
            state.atomic_write_json(
                rpaths.owner,
                {"schema_version": 1, "run_id": replacement_run_id, "name": run_name},
            )
            state.atomic_write_json(rpaths.state, replacement_state)
        return {"owner": {"run_id": old_run_id}, "state": {"backend": "kvm", "status": "stopped"}}

    callbacks = replace(
        callbacks,
        inspect=racing_inspect,
        resolve=lambda *_args: object(),
        preflight=lambda _items: None,
        prepare=lambda _items: (),
    )
    effects: list[str] = []
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.cloud_runtime,
        "start",
        lambda *_args, **_kwargs: effects.append("cloud"),
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.lima,
        "start",
        lambda *_args, **_kwargs: effects.append("lima"),
    )

    with pytest.raises(ProjectLifecycleError, match="run identity changed before lifecycle operation") as captured:
        # The orchestration layer wraps the stable dispatcher error in its existing
        # project-up failure type after completing a no-op rollback.
        up_project(project, callbacks, roots=roots, services=["api"])

    assert effects == []
    assert inspections == 2
    assert project_state.read_bytes() == project_before
    assert state.read_run_dispatch_record(roots, run_name).run_id == replacement_run_id
    rendered = str(captured.value)
    assert "SENSITIVE_REPLACEMENT_VALUE" not in rendered


@pytest.mark.parametrize("operation", ["inspect", "remove"])
def test_project_callbacks_treat_dangling_run_symlink_as_ambiguous_and_fail_in_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    (roots.runs / run_name).symlink_to("missing-run", target_is_directory=True)
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    effects: list[str] = []

    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"side effect reached: {effect}")

    monkeypatch.setattr(project_adapter.lima, "available", lambda: forbidden("foreign-probe"))
    target_name = "inspect_run" if operation == "inspect" else "rm"
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.cloud_runtime,
        target_name,
        lambda *_args, **_kwargs: forbidden("cloud-backend"),
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.lima,
        target_name,
        lambda *_args, **_kwargs: forbidden("lima-backend"),
    )

    with pytest.raises(StateError, match="cannot securely read run ledger"):
        callbacks.inspect(run_name) if operation == "inspect" else callbacks.remove(run_name)
    assert effects == []


@pytest.mark.parametrize("operation", ["inspect", "remove"])
@pytest.mark.parametrize("parent_kind", ["symlink", "non-directory", "swap"])
def test_project_callbacks_reject_ambiguous_runs_parent_before_dispatch_or_foreign_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    parent_kind: str,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    if parent_kind == "symlink":
        roots.runs.rename(roots.state / "real-runs")
        roots.runs.symlink_to("real-runs", target_is_directory=True)
    elif parent_kind == "non-directory":
        roots.runs.rmdir()
        roots.runs.write_text("not a directory", encoding="utf-8")
    else:
        original_fstat = state._safe_fstat
        calls = 0

        def swapping_fstat(file_fd: int) -> object:
            nonlocal calls
            result = original_fstat(file_fd)
            calls += 1
            if calls == 1:
                roots.runs.rename(roots.state / "old-runs")
                roots.runs.mkdir()
            return result

        monkeypatch.setattr(state, "_safe_fstat", swapping_fstat)
    effects: list[str] = []

    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"side effect reached: {effect}")

    monkeypatch.setattr(project_adapter.lima, "available", lambda: forbidden("foreign-probe"))
    dispatcher_name = "inspect_run" if operation == "inspect" else "rm"
    monkeypatch.setattr(
        project_adapter.runtime_dispatch,
        dispatcher_name,
        lambda *_args, **_kwargs: forbidden("dispatcher"),
    )

    with pytest.raises(StateError, match="cannot securely inspect run entry") as captured:
        callbacks.inspect(run_name) if operation == "inspect" else callbacks.remove(run_name)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert effects == []


@pytest.mark.parametrize("parent_kind", ["symlink", "non-directory", "swap"])
def test_down_project_rejects_ambiguous_runs_parent_before_dispatch_backend_or_ledger_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_kind: str,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    project_state = _write_project_service_ledger(project, roots, run_name=run_name, run_id=str(uuid.uuid4()))
    before = project_state.read_bytes()
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    if parent_kind == "symlink":
        roots.runs.rename(roots.state / "real-runs")
        roots.runs.symlink_to("real-runs", target_is_directory=True)
    elif parent_kind == "non-directory":
        roots.runs.rmdir()
        roots.runs.write_text("not a directory", encoding="utf-8")
    else:
        original_fstat = state._safe_fstat
        calls = 0

        def swapping_fstat(file_fd: int) -> object:
            nonlocal calls
            result = original_fstat(file_fd)
            calls += 1
            if calls == 1:
                roots.runs.rename(roots.state / "old-runs")
                roots.runs.mkdir()
            return result

        monkeypatch.setattr(state, "_safe_fstat", swapping_fstat)
    effects: list[str] = []

    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"side effect reached: {effect}")

    monkeypatch.setattr(project_adapter.lima, "available", lambda: forbidden("foreign-probe"))
    for dispatcher_name in ("inspect_run", "stop", "rm"):
        monkeypatch.setattr(
            project_adapter.runtime_dispatch,
            dispatcher_name,
            lambda *_args, _name=dispatcher_name, **_kwargs: forbidden(f"dispatcher-{_name}"),
        )

    with pytest.raises(StateError, match="cannot securely inspect run entry") as captured:
        down_project(project, callbacks, roots=roots)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert effects == []
    assert project_state.read_bytes() == before


def test_down_project_dangling_run_symlink_fails_in_dispatcher_before_backend_or_ledger_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    project_state = _write_project_service_ledger(project, roots, run_name=run_name, run_id=str(uuid.uuid4()))
    before = project_state.read_bytes()
    (roots.runs / run_name).symlink_to("missing-run", target_is_directory=True)
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    effects: list[str] = []

    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"side effect reached: {effect}")

    monkeypatch.setattr(project_adapter.lima, "available", lambda: forbidden("foreign-probe"))
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.cloud_runtime,
        "inspect_run",
        lambda *_args, **_kwargs: forbidden("cloud-backend"),
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.lima,
        "inspect_run",
        lambda *_args, **_kwargs: forbidden("lima-backend"),
    )

    with pytest.raises(StateError, match="cannot securely read run ledger"):
        down_project(project, callbacks, roots=roots)
    assert effects == []
    assert project_state.read_bytes() == before


@pytest.mark.parametrize("operation", ["inspect", "start", "stop", "remove", "logs"])
def test_project_callbacks_fail_closed_on_partial_or_oci_run_ledgers_before_backend_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    run_name = service_run_name(project, "api")
    rpaths = _write_run_ledger(roots, run_name, backend="kvm", runtime_kind="oci-root")
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    effects: list[str] = []

    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"backend side effect reached: {effect}")

    target_name = {"inspect": "inspect_run", "start": "start", "stop": "stop", "remove": "rm", "logs": "logs"}[
        operation
    ]
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.cloud_runtime,
        target_name,
        lambda *_args, **_kwargs: forbidden("cloud"),
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.lima,
        target_name,
        lambda *_args, **_kwargs: forbidden("lima"),
    )
    monkeypatch.setattr(
        project_adapter.lima,
        "is_lima_run",
        lambda *_args: forbidden("legacy-heuristic"),
    )

    def invoke() -> object:
        if operation == "inspect":
            return callbacks.inspect(run_name)
        if operation == "start":
            service = project.services["api"]
            return callbacks.start(
                PreparedService(
                    project,
                    service,
                    ServicePlan("api", run_name, "start", "sha256:" + "b" * 64, "stopped"),
                    None,
                )
            )
        if operation == "stop":
            return callbacks.stop(run_name)
        if operation == "remove":
            return callbacks.remove(run_name)
        return list(callbacks.logs(run_name, False))

    with pytest.raises(RuntimeCapabilityError):
        invoke()
    assert effects == []

    rpaths.state.write_text('{"schema_version":"corrupt"}\n', encoding="utf-8")
    with pytest.raises(StateError, match="invalid run state schema"):
        invoke()
    assert effects == []

    rpaths.owner.unlink()
    with pytest.raises(StateError, match="cannot securely read run ledger"):
        invoke()
    assert effects == []


def test_volume_deletion_uses_ledger_backend_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    roots = _roots(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        project_adapter,
        "delete_kvm_volume",
        lambda *_args, **_kwargs: calls.append("kvm") or True,
    )
    monkeypatch.setattr(
        project_adapter,
        "delete_lima_volume",
        lambda *_args, **_kwargs: calls.append("lima") or True,
    )
    monkeypatch.setattr(project_adapter, "kvm_volume_path", lambda *_args: tmp_path / "missing.raw")
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))

    callbacks.remove_volume(project.name, "data", "kvm", 1024 * 1024 * 1024)

    assert calls == ["kvm"]


def test_managed_default_network_rejects_non_nat_driver_before_backend_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    project = _project(
        tmp_path,
        f"""networks:
  default:
    driver: isolated
services:
  api:
    image: sha256:{"a" * 64}
""",
    )
    callbacks = project_adapter.build_project_callbacks(project, _roots(tmp_path), lambda _service: _stack(tmp_path))

    with pytest.raises(ArtifactValidationError, match="requires driver 'nat'"):
        callbacks.resolve(project, project.services["api"], service_run_name(project, "api"))


def test_kvm_preflight_rejects_live_name_collision_before_volume_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    project = _project(
        tmp_path,
        f"""volumes:
  data:
    size: 1GiB
services:
  api:
    image: sha256:{"a" * 64}
    volumes: [data:/srv/data]
""",
    )
    callbacks = project_adapter.build_project_callbacks(project, _roots(tmp_path), lambda _service: _stack(tmp_path))
    service = project.services["api"]
    run_name = service_run_name(project, "api")
    resolved = callbacks.resolve(project, service, run_name)
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", run_name, "create", "sha256:" + "b" * 64, None),
        resolved,
    )
    monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
    monkeypatch.setattr(
        project_adapter.kvm,
        "validate_domain_name_available",
        lambda _name: (_ for _ in ()).throw(RuntimeError("domain collision")),
    )
    created: list[str] = []
    monkeypatch.setattr(
        project_adapter,
        "ensure_kvm_volume",
        lambda *_args, **_kwargs: created.append("volume") or pytest.fail("volume mutated"),
    )

    with pytest.raises(RuntimeError, match="domain collision"):
        callbacks.preflight((prepared,))
    assert created == []


def test_lima_new_disk_formats_once_and_existing_disk_never_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""volumes:
  app_data:
    size: 1GiB
services:
  api:
    image: sha256:{"a" * 64}
    volumes: [app_data:/srv/data]
""",
    )
    roots = _roots(tmp_path)
    service = project.services["api"]
    run_name = service_run_name(project, "api")
    backend_name = project_adapter.lima_backend_name(project.name, "app_data")
    assert len(backend_name) == 11
    monkeypatch.setattr(project_adapter.lima, "available", lambda: True)
    monkeypatch.setattr(project_adapter.lima, "validate_network", lambda _name: None)
    monkeypatch.setattr(project_adapter.lima, "validate_run_spec", lambda _spec: None)

    existing: object | None = None

    def verify(*_args: object, **_kwargs: object) -> object | None:
        return existing

    monkeypatch.setattr(project_adapter, "verify_lima_volume", verify)
    monkeypatch.setattr(
        project_adapter,
        "ensure_lima_volume",
        lambda *_args, **_kwargs: SimpleNamespace(backend_name=backend_name, created=True),
    )
    received: list[object] = []
    monkeypatch.setattr(
        project_adapter.runtime_dispatch,
        "run",
        lambda request, **_kwargs: received.append(request.spec) or {"status": "running"},
    )
    callbacks = project_adapter.build_project_callbacks(
        project,
        roots,
        lambda _service: _stack(tmp_path, arch="aarch64"),
    )
    resolved = callbacks.resolve(project, service, run_name)
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", run_name, "create", "sha256:" + "b" * 64, None),
        resolved,
    )
    callbacks.preflight((prepared,))
    callbacks.prepare((prepared,))
    callbacks.start(prepared)
    assert received[-1].volumes[0].name == "app_data"
    assert received[-1].volumes[0].backend_name == backend_name
    assert received[-1].volumes[0].format is True

    existing = SimpleNamespace(backend_name=backend_name)
    callbacks = project_adapter.build_project_callbacks(
        project,
        roots,
        lambda _service: _stack(tmp_path, arch="aarch64"),
    )
    resolved = callbacks.resolve(project, service, run_name)
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", run_name, "create", "sha256:" + "c" * 64, None),
        resolved,
    )
    callbacks.preflight((prepared,))
    callbacks.prepare((prepared,))
    callbacks.start(prepared)
    assert received[-1].volumes[0].format is False

    monkeypatch.setattr(
        project_adapter.runtime_dispatch,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LifecycleError("ssh handshake failed")),
    )
    with pytest.raises(LifecycleError, match="ssh handshake failed.*inspect the original failure"):
        callbacks.start(prepared)


def test_applied_read_only_volume_and_port_reservations_block_new_service_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""volumes:
  data:
    size: 1GiB
services:
  old:
    image: sha256:{"a" * 64}
  new:
    image: sha256:{"a" * 64}
    volumes: [data:/srv/data]
    ports: [18080:8080]
""",
    )
    roots = _roots(tmp_path)
    old_run = service_run_name(project, "old")
    rpaths = state.run_paths(roots, old_run)
    rpaths.root.mkdir(parents=True, mode=0o700)
    owner = state.write_owner_record(rpaths)
    backend_name = project_adapter.lima_backend_name(project.name, "data")
    state.write_run_state(
        rpaths,
        status="running",
        data={
            "backend": "lima-vz",
            "volumes": [
                {
                    "name": "data",
                    "backend_name": backend_name,
                    "mount_path": "/srv/data",
                    "filesystem": "ext4",
                    "read_only": True,
                }
            ],
            "ports": [
                {
                    "host_ip": "127.0.0.1",
                    "host_port": 18080,
                    "guest_port": 8080,
                    "protocol": "tcp",
                }
            ],
        },
    )
    ppaths = state.project_paths(roots, project.name)
    ppaths.root.mkdir(parents=True, mode=0o700)
    now = state.utc_now_iso()
    state.atomic_write_json(
        ppaths.state,
        {
            "schema_version": 2,
            "project": project.name,
            "config_digest": project_config_digest(project),
            "services": [
                {
                    "service": "old",
                    "run_name": old_run,
                    "config_digest": "sha256:" + "d" * 64,
                    "run_id": owner.run_id,
                    "backend": "lima-vz",
                }
            ],
            "order": ["old"],
            "volumes": [{"name": "data", "backend": "lima-vz", "size_bytes": 1024**3}],
            "created_at": now,
            "updated_at": now,
        },
    )
    monkeypatch.setattr(project_adapter.lima, "available", lambda: True)
    callbacks = project_adapter.build_project_callbacks(
        project,
        roots,
        lambda _service: _stack(tmp_path, arch="aarch64"),
    )
    service = project.services["new"]
    run_name = service_run_name(project, "new")
    prepared = PreparedService(
        project,
        service,
        ServicePlan("new", run_name, "create", "sha256:" + uuid.uuid4().hex.ljust(64, "0"), None),
        callbacks.resolve(project, service, run_name),
    )

    with pytest.raises(ArtifactValidationError, match="already reserved"):
        callbacks.preflight((prepared,))


def test_existing_kvm_volume_disappearing_after_preflight_is_never_recreated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""volumes:
  data:
    size: 1GiB
services:
  api:
    image: sha256:{"a" * 64}
    volumes: [data:/srv/data]
""",
    )
    roots = _roots(tmp_path)
    volume_path = tmp_path / "data.raw"
    volume_path.write_bytes(b"owned")
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
    monkeypatch.setattr(project_adapter.kvm, "validate_domain_name_available", lambda _name: None)
    monkeypatch.setattr(project_adapter, "kvm_volume_path", lambda *_args: volume_path)

    def verify(*_args: object, **_kwargs: object) -> object:
        if not volume_path.exists():
            raise StateError("volume disappeared")
        return SimpleNamespace(path=volume_path)

    monkeypatch.setattr(project_adapter, "verify_kvm_volume", verify)
    monkeypatch.setattr(
        project_adapter,
        "ensure_kvm_volume",
        lambda *_args, **_kwargs: pytest.fail("pre-existing volume must not be recreated"),
    )
    callbacks = project_adapter.build_project_callbacks(project, roots, lambda _service: _stack(tmp_path))
    service = project.services["api"]
    run_name = service_run_name(project, "api")
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", run_name, "create", "sha256:" + "e" * 64, None),
        callbacks.resolve(project, service, run_name),
    )

    callbacks.preflight((prepared,))
    volume_path.unlink()
    with pytest.raises(ProjectPrepareError, match="volume disappeared"):
        callbacks.prepare((prepared,))


def test_invalid_final_run_spec_is_rejected_during_read_only_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
    environment:
      BAD_VALUE: |-
        first
        second
""",
    )
    monkeypatch.setattr(project_adapter.runtime_dispatch.platforms, "select_backend", lambda _arch, **_kw: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    callbacks = project_adapter.build_project_callbacks(project, _roots(tmp_path), lambda _service: _stack(tmp_path))
    service = project.services["api"]
    run_name = service_run_name(project, "api")
    with pytest.raises(ArtifactValidationError, match="single NUL-free line"):
        callbacks.resolve(project, service, run_name)


def test_project_resolution_uses_typed_runtime_dispatch_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, f"services:\n  api:\n    image: sha256:{'a' * 64}\n")
    selected: list[tuple[str, str]] = []
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.platforms,
        "select_backend",
        lambda arch, requested="auto": selected.append((arch, requested)) or "kvm",
    )
    callbacks = project_adapter.build_project_callbacks(
        project,
        _roots(tmp_path),
        lambda _service: _stack(tmp_path),
    )

    resolved = callbacks.resolve(project, project.services["api"], "demo-api-1")

    assert resolved.request.dispatch_key == DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    assert resolved.request.spec.name == "demo-api-1"
    assert resolved.request.volume_intents == ()
    assert selected == [("x86_64", "auto"), ("x86_64", "kvm")]


def test_project_resolution_preserves_ordered_nonreflective_volume_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        f"""volumes:
  alpha: {{size: 1GiB}}
  beta: {{size: 1GiB}}
services:
  api:
    image: sha256:{"a" * 64}
    volumes:
      - alpha:/srv/alpha:ro
      - beta:/srv/beta
""",
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.platforms,
        "select_backend",
        lambda _arch, **_kwargs: "kvm",
    )
    callbacks = project_adapter.build_project_callbacks(project, _roots(tmp_path), lambda _service: _stack(tmp_path))

    resolved = callbacks.resolve(project, project.services["api"], "demo-api-1")

    assert resolved.request.attachments_bound is False
    assert resolved.request.spec.volumes == ()
    assert resolved.request.volume_intents == (
        RunVolumeIntent("alpha", "/srv/alpha", "ext4", True),
        RunVolumeIntent("beta", "/srv/beta", "ext4", False),
    )
    assert "/srv/alpha" not in repr(resolved)
    assert "alpha" not in repr(resolved)


def test_libvirt_hvf_backend_routes_like_kvm_for_ports_and_volumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(
        tmp_path,
        f"""services:
  api:
    image: sha256:{"a" * 64}
    ports: ["18080:8080"]
""",
    )
    monkeypatch.setattr(
        project_adapter.runtime_dispatch.platforms,
        "select_backend",
        lambda _arch, **_kw: "libvirt-hvf",
    )
    monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
    callbacks = project_adapter.build_project_callbacks(
        project,
        _roots(tmp_path),
        lambda _service: _stack(tmp_path, arch="aarch64"),
    )
    service = project.services["api"]
    resolved = callbacks.resolve(project, service, "demo-api-1")
    assert resolved.backend == "libvirt-hvf"
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", "demo-api-1", "create", "sha256:" + "b" * 64, None),
        resolved,
    )

    with pytest.raises(ArtifactValidationError, match="per-domain inbound forwarding"):
        callbacks.preflight((prepared,))


def test_kvm_volume_reference_check_allows_only_exact_owned_teardown_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = str(uuid.uuid4())
    path = tmp_path / "data.raw"
    path.write_bytes(b"data")

    class Domain:
        def __init__(self, marker: str) -> None:
            self.marker = marker

        def metadata(self, *_args: object) -> str:
            return (
                f'<palimpsest:run xmlns:palimpsest="{project_adapter.kvm.DOMAIN_MARKER_NAMESPACE}" '
                f'id="{self.marker}" schema="1" version="{project_adapter.kvm.DOMAIN_MARKER_VERSION}" />'
            )

        def XMLDesc(self, *_args: object) -> str:
            return (
                '<domain xmlns:palimpsest="https://afterglow.dev/palimpsest-local/domain/v1">'
                "<name>demo-api-1</name><metadata>"
                f'<palimpsest:run id="{self.marker}" schema="1" '
                f'version="{project_adapter.kvm.DOMAIN_MARKER_VERSION}" />'
                "</metadata><devices><disk>"
                f'<source file="{path}"/>'
                "</disk></devices></domain>"
            )

    connection = SimpleNamespace(listAllDomains=lambda _flags: [Domain(run_id)], close=lambda: None)
    monkeypatch.setattr(project_adapter.kvm, "connect", lambda _uri: connection)

    project_adapter._validate_kvm_volume_references(path, {"demo-api-1": run_id})
    with pytest.raises(StateError, match="foreign or unexpected"):
        project_adapter._validate_kvm_volume_references(path, {})
