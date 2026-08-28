"""Runtime adapter contracts for declarative projects."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import project_adapter, state
from palimpsest_local.errors import ArtifactValidationError, LifecycleError, StateError
from palimpsest_local.project import load_project
from palimpsest_local.project_runtime import (
    PreparedService,
    ProjectPrepareError,
    ServicePlan,
    project_config_digest,
    service_run_name,
)
from palimpsest_local.refs import ImageRef, StackRef


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


def test_desired_digest_binds_resolved_environment_and_runtime_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "kvm")
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
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
    monkeypatch.setattr(project_adapter, "preflight_kvm_volume_support", lambda: None)
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
    volume_path.write_bytes(b"raw-volume")
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
    monkeypatch.setattr(project_adapter.kvm, "validate_domain_name_available", lambda _name: None)
    monkeypatch.setattr(project_adapter, "kvm_volume_path", lambda *_args: volume_path)
    monkeypatch.setattr(
        project_adapter,
        "verify_kvm_volume",
        lambda *_args, **_kwargs: SimpleNamespace(path=volume_path),
    )
    monkeypatch.setattr(
        project_adapter,
        "ensure_kvm_volume",
        lambda *_args, **_kwargs: SimpleNamespace(path=volume_path),
    )
    received: list[object] = []
    monkeypatch.setattr(
        project_adapter.runtime,
        "run",
        lambda spec, **_kwargs: received.append(spec) or {"status": "running"},
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

    spec = received[0]
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
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    monkeypatch.setattr(project_adapter.lima, "is_lima_run", lambda _paths: True)
    calls: list[str] = []
    monkeypatch.setattr(
        project_adapter.lima,
        "start",
        lambda name, **_kwargs: calls.append(name) or {"status": "running"},
    )
    monkeypatch.setattr(project_adapter.runtime, "start", lambda *_args, **_kwargs: pytest.fail("wrong backend"))
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
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "kvm")
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
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "kvm")
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
        project_adapter.lima,
        "run",
        lambda spec, **_kwargs: received.append(spec) or {"status": "running"},
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
        project_adapter.lima,
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
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "kvm")
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
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "kvm")
    monkeypatch.setattr(project_adapter.lima, "available", lambda: False)
    callbacks = project_adapter.build_project_callbacks(project, _roots(tmp_path), lambda _service: _stack(tmp_path))
    service = project.services["api"]
    run_name = service_run_name(project, "api")
    prepared = PreparedService(
        project,
        service,
        ServicePlan("api", run_name, "recreate", "sha256:" + "f" * 64, "running"),
        callbacks.resolve(project, service, run_name),
    )

    with pytest.raises(ArtifactValidationError, match="single NUL-free line"):
        callbacks.preflight((prepared,))


def test_backend_for_stack_uses_platforms_select_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stack = _stack(tmp_path)
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda arch: "lima-vz")
    assert project_adapter._backend_for_stack(stack) == "lima-vz"

    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda arch: "libvirt-hvf")
    assert project_adapter._backend_for_stack(stack) == "libvirt-hvf"

    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda arch: "kvm")
    assert project_adapter._backend_for_stack(stack) == "kvm"


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
    monkeypatch.setattr(project_adapter.platforms, "select_backend", lambda _arch: "libvirt-hvf")
    monkeypatch.setattr(project_adapter.kvm, "validate_network", lambda _name: None)
    callbacks = project_adapter.build_project_callbacks(project, _roots(tmp_path), lambda _service: _stack(tmp_path))
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
            return f'<run id="{self.marker}"/>'

        def XMLDesc(self, *_args: object) -> str:
            return (
                '<domain xmlns:palimpsest="https://afterglow.dev/palimpsest-local/domain/v1">'
                "<name>demo-api-1</name><metadata>"
                f'<palimpsest:run id="{self.marker}"/>'
                "</metadata><devices><disk>"
                f'<source file="{path}"/>'
                "</disk></devices></domain>"
            )

    connection = SimpleNamespace(listAllDomains=lambda _flags: [Domain(run_id)], close=lambda: None)
    monkeypatch.setattr(project_adapter.kvm, "connect", lambda _uri: connection)

    project_adapter._validate_kvm_volume_references(path, {"demo-api-1": run_id})
    with pytest.raises(StateError, match="foreign or unexpected"):
        project_adapter._validate_kvm_volume_references(path, {})
