"""Tests for transactional declarative project orchestration."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

import palimpsest_local.state as state
from palimpsest_local.errors import StateError
from palimpsest_local.project import MountSpec, NetworkSpec, PortSpec, Project, ServiceSpec, VolumeSpec
from palimpsest_local.project_runtime import (
    PROJECT_STATE_SCHEMA_VERSION,
    ManagedVolume,
    ProjectCallbacks,
    ProjectLifecycleError,
    ProjectPrepareError,
    down_project,
    managed_run_name,
    project_log_targets,
    project_logs,
    project_ps,
    project_service_operation,
    published_ports,
    read_project_state,
    service_config_digest,
    service_run_name,
    stop_project_services,
    up_project,
)

_IMAGE = "sha256:" + "a" * 64


def _service(
    name: str,
    *,
    depends_on: tuple[str, ...] = (),
    mounts: tuple[MountSpec, ...] = (),
    ports: tuple[PortSpec, ...] = (),
    memory_mib: int = 4096,
) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        image=_IMAGE,
        bundle=None,
        layers=(),
        memory_mib=memory_mib,
        vcpus=2,
        networks=("default",),
        volumes=mounts,
        ports=ports,
        environment=MappingProxyType({"ACCESS_TOKEN": "${ACCESS_TOKEN:?required}"}),
        env_files=(),
        cloud_init=None,
        depends_on=depends_on,
    )


def _project(tmp_path: Path, *, api_memory: int = 4096) -> Project:
    services = {
        "db": _service("db", mounts=(MountSpec("volume", "data", "/var/lib/data"),)),
        "api": _service(
            "api",
            depends_on=("db",),
            ports=(PortSpec(18080, 8080),),
            memory_mib=api_memory,
        ),
        "worker": _service("worker", depends_on=("api",)),
    }
    return Project(
        version="1",
        name="demo",
        source=tmp_path / "palimpsest.yml",
        root=tmp_path,
        services=MappingProxyType(services),
        networks=MappingProxyType({"default": NetworkSpec("default")}),
        volumes=MappingProxyType({"data": VolumeSpec("data", size_mib=1024)}),
    )


def _roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})


class FakeRuntime:
    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}
        self.run_ids: dict[str, str] = {}
        self.backends: dict[str, str] = {}
        self.generation = 0
        self.events: list[tuple[object, ...]] = []
        self.fail_start: str | None = None
        self.fail_preflight = False
        self.fail_prepare = False
        self.unavailable_port: int | None = None

    def inspect(self, name: str) -> object | None:
        self.events.append(("inspect", name))
        status = self.statuses.get(name)
        if status is None:
            return None
        run_id = self.run_ids.setdefault(name, str(uuid.uuid5(uuid.NAMESPACE_URL, f"foreign:{name}")))
        backend = self.backends.setdefault(name, "kvm")
        return {"owner": {"run_id": run_id}, "state": {"status": status, "backend": backend}}

    def resolve(self, _project: Project, service: ServiceSpec, run_name: str) -> object:
        self.events.append(("resolve", service.name))
        # Deliberately secret-shaped to prove opaque resolution never reaches state.
        return {"runtime_credential": f"secret-for-{run_name}"}

    def preflight(self, services: tuple[object, ...]) -> None:
        self.events.append(("preflight", tuple(item.plan.service for item in services)))
        if self.fail_preflight:
            raise RuntimeError("preflight rejected")

    def prepare(self, services: tuple[object, ...]) -> tuple[ManagedVolume, ...]:
        self.events.append(("prepare", tuple(item.plan.service for item in services)))
        volumes: dict[str, ManagedVolume] = {}
        for item in services:
            for mount in item.service.volumes:
                volume = item.project.volumes[mount.source]
                if not volume.external:
                    volumes[volume.name] = ManagedVolume(volume.name, "kvm", volume.size_mib * 1024 * 1024)
        if self.fail_prepare:
            raise ProjectPrepareError("prepare rejected", tuple(volumes.values()))
        return tuple(volumes[name] for name in sorted(volumes))

    def start(self, prepared: object) -> None:
        name = prepared.plan.run_name
        self.events.append(("start", prepared.plan.service, prepared.plan.action))
        if prepared.plan.action in {"create", "recreate"} or name not in self.run_ids:
            self.generation += 1
            self.run_ids[name] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{name}:{self.generation}"))
            self.backends[name] = "kvm"
        if prepared.plan.service == self.fail_start:
            self.statuses[name] = "failed"
            raise RuntimeError("start failed")
        self.statuses[name] = "running"

    def stop(self, name: str) -> None:
        self.events.append(("stop", name))
        if name in self.statuses:
            self.statuses[name] = "stopped"

    def remove(self, name: str) -> None:
        self.events.append(("remove", name))
        self.statuses.pop(name, None)
        self.run_ids.pop(name, None)
        self.backends.pop(name, None)

    def port_available(self, port: object, replacing: str | None) -> bool:
        self.events.append(("port", port.host_port, replacing))
        return port.host_port != self.unavailable_port

    def remove_volume(self, project: str, volume: str, backend: str, size_bytes: int) -> None:
        self.events.append(("remove-volume", project, volume, backend, size_bytes))

    def logs(self, run_name: str, follow: bool):
        self.events.append(("logs", run_name, follow))
        yield "first\n"
        yield "second\n"

    def callbacks(self) -> ProjectCallbacks:
        return ProjectCallbacks(
            inspect=self.inspect,
            resolve=self.resolve,
            start=self.start,
            stop=self.stop,
            remove=self.remove,
            preflight=self.preflight,
            prepare=self.prepare,
            port_available=self.port_available,
            remove_volume=self.remove_volume,
            logs=self.logs,
        )


def test_deterministic_names_hashes_and_port_query(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert service_run_name(project, "api") == "demo-api-1"
    assert service_config_digest(project, "api").startswith("sha256:")
    assert service_config_digest(project, "api") != service_config_digest(_project(tmp_path, api_memory=8192), "api")
    assert published_ports(project) == (
        # Port queries expose logical and runtime ownership together.
        replace(
            published_ports(project, ["api"])[0],
            service="api",
            run_name="demo-api-1",
        ),
    )


def test_up_resolves_and_preflights_every_service_before_first_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)

    result = up_project(project, runtime.callbacks(), roots=roots, services=["worker"])

    assert [item.service for item in result.actions] == ["db", "api", "worker"]
    assert [item.action for item in result.actions] == ["create", "create", "create"]
    first_start = next(index for index, event in enumerate(runtime.events) if event[0] == "start")
    assert [event for event in runtime.events[:first_start] if event[0] == "resolve"] == [
        ("resolve", "db"),
        ("resolve", "api"),
        ("resolve", "worker"),
    ]
    assert ("preflight", ("db", "api", "worker")) in runtime.events[:first_start]
    assert ("prepare", ("db", "api", "worker")) in runtime.events[:first_start]
    assert runtime.events.index(("preflight", ("db", "api", "worker"))) < runtime.events.index(
        ("prepare", ("db", "api", "worker"))
    )
    assert [event[1] for event in runtime.events if event[0] == "start"] == ["db", "api", "worker"]
    ledger = read_project_state(project, roots)
    assert ledger is not None
    assert ledger.schema_version == PROJECT_STATE_SCHEMA_VERSION
    assert ledger.order == ("db", "api", "worker")
    assert set(ledger.volumes) == {"data"}
    assert ledger.volumes["data"].backend == "kvm"
    assert ledger.volumes["data"].size_bytes == 1024 * 1024 * 1024
    raw_state = state.project_paths(roots, "demo").state.read_text(encoding="utf-8")
    assert "secret-for" not in raw_state
    assert "ACCESS_TOKEN" not in raw_state


def test_same_config_running_is_a_true_noop(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(project, runtime.callbacks(), roots=roots, services=["api"])
    before = state.project_paths(roots, project.name).state.read_bytes()
    runtime.events.clear()

    result = up_project(project, runtime.callbacks(), roots=roots, services=["api"])

    assert not result.changed
    assert all(item.action == "noop" for item in result.actions)
    assert {event[0] for event in runtime.events} == {"inspect"}
    assert state.project_paths(roots, project.name).state.read_bytes() == before


def test_secret_shaped_service_name_is_a_value_not_a_state_field(tmp_path: Path) -> None:
    base = _project(tmp_path)
    project = replace(base, services=MappingProxyType({"secret-agent": _service("secret-agent")}))
    runtime = FakeRuntime()
    roots = _roots(tmp_path)

    up_project(project, runtime.callbacks(), roots=roots)

    ledger = read_project_state(project, roots)
    assert ledger is not None and set(ledger.services) == {"secret-agent"}


def test_preflight_or_port_failure_happens_before_mutation_and_state_write(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    runtime.unavailable_port = 18080
    roots = _roots(tmp_path)

    with pytest.raises(ProjectLifecycleError, match="unavailable"):
        up_project(project, runtime.callbacks(), roots=roots, services=["api"])

    assert not any(event[0] in {"start", "stop", "remove"} for event in runtime.events)
    assert not state.project_paths(roots, project.name).state.exists()

    runtime.unavailable_port = None
    runtime.fail_preflight = True
    runtime.events.clear()
    with pytest.raises(RuntimeError, match="preflight rejected"):
        up_project(project, runtime.callbacks(), roots=roots, services=["api"])
    assert not any(event[0] in {"start", "stop", "remove"} for event in runtime.events)
    assert not state.project_paths(roots, project.name).state.exists()


def test_stopped_service_rechecks_port_and_transitional_status_fails_closed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(project, runtime.callbacks(), roots=roots, services=["api"])
    runtime.statuses["demo-api-1"] = "stopped"
    runtime.unavailable_port = 18080
    runtime.events.clear()

    with pytest.raises(ProjectLifecycleError, match="unavailable"):
        up_project(project, runtime.callbacks(), roots=roots, services=["api"])
    api_port_checks = [event for event in runtime.events if event[:2] == ("port", 18080)]
    assert api_port_checks == [("port", 18080, None)]
    assert not any(event[0] == "start" for event in runtime.events)

    runtime.unavailable_port = None
    runtime.statuses["demo-api-1"] = "starting"
    runtime.events.clear()
    with pytest.raises(ProjectLifecycleError, match="transitional status"):
        up_project(project, runtime.callbacks(), roots=roots, services=["api"])
    assert not any(event[0] in {"resolve", "start", "stop", "remove"} for event in runtime.events)


def test_drift_no_recreate_and_force_recreate_modes(tmp_path: Path) -> None:
    original = _project(tmp_path)
    changed = _project(tmp_path, api_memory=8192)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(original, runtime.callbacks(), roots=roots, services=["api"])

    runtime.events.clear()
    no_recreate = up_project(changed, runtime.callbacks(), roots=roots, services=["api"], no_recreate=True)
    assert [item.action for item in no_recreate.actions] == ["noop", "noop"]
    assert not any(event[0] in {"resolve", "start", "stop", "remove"} for event in runtime.events)

    runtime.events.clear()
    recreated = up_project(changed, runtime.callbacks(), roots=roots, services=["api"])
    assert [item.action for item in recreated.actions] == ["noop", "recreate"]
    assert ("stop", "demo-api-1") in runtime.events
    assert ("remove", "demo-api-1") in runtime.events
    assert ("start", "api", "recreate") in runtime.events

    runtime.events.clear()
    forced = up_project(changed, runtime.callbacks(), roots=roots, services=["api"], force_recreate=True)
    assert [item.action for item in forced.actions] == ["recreate", "recreate"]
    assert [event[1] for event in runtime.events if event[0] == "start"] == ["db", "api"]


def test_execution_only_digest_change_recreates_without_persisting_secret(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    execution_values = {"db": "stable", "api": "raw-secret-one", "worker": "stable"}

    def desired_digest(active_project: Project, service: ServiceSpec) -> str:
        payload = f"{service_config_digest(active_project, service.name)}\0{execution_values[service.name]}"
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    callbacks = replace(runtime.callbacks(), desired_digest=desired_digest)
    up_project(project, callbacks, roots=roots, services=["api"])
    raw_state = state.project_paths(roots, project.name).state.read_text(encoding="utf-8")
    assert "raw-secret-one" not in raw_state

    execution_values["api"] = "raw-secret-two"
    runtime.events.clear()
    result = up_project(project, callbacks, roots=roots, services=["api"])

    assert [item.action for item in result.actions] == ["noop", "recreate"]
    assert "raw-secret-two" not in state.project_paths(roots, project.name).state.read_text(encoding="utf-8")


def test_rollback_touches_only_services_created_by_this_invocation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(project, runtime.callbacks(), roots=roots, services=["db"])
    runtime.events.clear()
    runtime.fail_start = "worker"

    with pytest.raises(ProjectLifecycleError, match="start failed"):
        up_project(project, runtime.callbacks(), roots=roots, services=["worker"])

    touched_by_rollback = [event[1] for event in runtime.events if event[0] in {"stop", "remove"}]
    assert "demo-db-1" not in touched_by_rollback
    assert touched_by_rollback == ["demo-worker-1", "demo-worker-1", "demo-api-1", "demo-api-1"]
    ledger = read_project_state(project, roots)
    assert ledger is not None
    assert set(ledger.services) == {"db"}
    assert runtime.statuses == {"demo-db-1": "running"}


def test_down_is_reverse_order_and_preserves_named_volumes_by_default(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(project, runtime.callbacks(), roots=roots)
    runtime.events.clear()

    result = down_project(project, runtime.callbacks(), roots=roots)

    assert result.removed_services == ("worker", "api", "db")
    assert [event[1] for event in runtime.events if event[0] == "remove"] == [
        "demo-worker-1",
        "demo-api-1",
        "demo-db-1",
    ]
    assert not any(event[0] == "remove-volume" for event in runtime.events)
    ledger = read_project_state(project, roots)
    assert ledger is not None and not ledger.services
    assert set(ledger.volumes) == {"data"}
    assert ledger.volumes["data"].backend == "kvm"
    assert ledger.volumes["data"].size_bytes == 1024 * 1024 * 1024

    runtime.events.clear()
    volume_result = down_project(project, runtime.callbacks(), roots=roots, volumes=True)
    assert volume_result.removed_services == ()
    assert volume_result.removed_volumes == ("data",)
    assert runtime.events == [("remove-volume", "demo", "data", "kvm", 1024 * 1024 * 1024)]


def test_down_calls_remove_for_owned_missing_runtime_tombstone(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(project, runtime.callbacks(), roots=roots, services=["db"])
    runtime.statuses.clear()
    runtime.events.clear()

    down_project(project, runtime.callbacks(), roots=roots)

    assert ("remove", "demo-db-1") in runtime.events
    assert not any(event[0] == "stop" for event in runtime.events)


def test_ps_log_and_exec_mapping_helpers(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(project, runtime.callbacks(), roots=roots, services=["api"])
    runtime.events.clear()

    statuses = project_ps(project, runtime.inspect, roots=roots)
    assert [(item.service, item.run_name, item.status) for item in statuses] == [
        ("db", "demo-db-1", "running"),
        ("api", "demo-api-1", "running"),
    ]
    assert project_log_targets(project, ["api"], roots=roots, inspect=runtime.inspect) == (("api", "demo-api-1"),)
    assert managed_run_name(project, "api", roots=roots, inspect=runtime.inspect) == "demo-api-1"
    assert list(project_logs(project, runtime.callbacks(), ["api"], roots=roots)) == [
        ("api", "first\n"),
        ("api", "second\n"),
    ]


def test_foreign_run_collision_and_corrupt_schema_fail_closed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    runtime.statuses["demo-db-1"] = "running"

    with pytest.raises(ProjectLifecycleError, match="not owned"):
        up_project(project, runtime.callbacks(), roots=roots, services=["db"])
    assert not any(event[0] in {"resolve", "start"} for event in runtime.events)

    ppaths = state.project_paths(roots, project.name)
    ppaths.root.mkdir(parents=True, exist_ok=True)
    ppaths.state.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "project": "demo",
                "config_digest": "sha256:" + "b" * 64,
                "services": {},
                "order": [],
                "volumes": [],
                "created_at": "now",
                "updated_at": "now",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StateError, match="schema version"):
        read_project_state(project, roots)

    corrupt = json.loads(ppaths.state.read_text(encoding="utf-8"))
    corrupt["schema_version"] = PROJECT_STATE_SCHEMA_VERSION
    corrupt["services"] = [
        {
            "service": "db",
            "run_name": "some-other-owned-looking-run",
            "config_digest": service_config_digest(project, "db"),
            "run_id": str(uuid.uuid4()),
            "backend": "kvm",
        }
    ]
    corrupt["order"] = ["db"]
    ppaths.state.write_text(json.dumps(corrupt), encoding="utf-8")
    with pytest.raises(StateError, match="expected 'demo-db-1'"):
        read_project_state(project, roots)


def test_reused_run_name_with_different_owner_identity_fails_closed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(project, runtime.callbacks(), roots=roots, services=["db"])
    runtime.run_ids["demo-db-1"] = str(uuid.uuid4())
    runtime.events.clear()

    with pytest.raises(ProjectLifecycleError, match="identity changed"):
        up_project(project, runtime.callbacks(), roots=roots, services=["db"])
    with pytest.raises(ProjectLifecycleError, match="identity changed"):
        down_project(project, runtime.callbacks(), roots=roots)
    with pytest.raises(ProjectLifecycleError, match="identity changed"):
        project_ps(project, runtime.inspect, roots=roots)
    with pytest.raises(ProjectLifecycleError, match="identity changed"):
        project_log_targets(project, ["db"], roots=roots, inspect=runtime.inspect)

    assert not any(event[0] in {"start", "stop", "remove"} for event in runtime.events)


def test_partial_volume_prepare_is_ledgered_without_vm_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    runtime.fail_prepare = True
    roots = _roots(tmp_path)

    with pytest.raises(ProjectLifecycleError, match="prepare rejected"):
        up_project(project, runtime.callbacks(), roots=roots, services=["db"])

    assert not any(event[0] in {"start", "stop", "remove"} for event in runtime.events)
    ledger = read_project_state(project, roots)
    assert ledger is not None
    assert not ledger.services
    assert ledger.volumes == {"data": ManagedVolume("data", "kvm", 1024 * 1024 * 1024)}


def test_no_recreate_preserves_running_config_but_rejects_stopped_restart(tmp_path: Path) -> None:
    original = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(original, runtime.callbacks(), roots=roots, services=["api"])

    changed_api = replace(
        original.services["api"],
        volumes=(MountSpec("volume", "new_data", "/srv/new"),),
        environment=MappingProxyType({"NEW_SECRET": "${NEW_SECRET:?required}"}),
    )
    changed = replace(
        original,
        services=MappingProxyType({**original.services, "api": changed_api}),
        volumes=MappingProxyType({**original.volumes, "new_data": VolumeSpec("new_data", size_mib=256)}),
    )

    def forbidden(*_args: object) -> object:
        raise AssertionError("current config must not be resolved under --no-recreate")

    callbacks = replace(runtime.callbacks(), resolve=forbidden, desired_digest=forbidden)
    runtime.events.clear()
    running = up_project(changed, callbacks, roots=roots, services=["api"], no_recreate=True)
    assert [plan.action for plan in running.actions] == ["noop", "noop"]
    assert not any(event[0] in {"resolve", "prepare", "start"} for event in runtime.events)

    runtime.statuses["demo-api-1"] = "stopped"
    runtime.events.clear()
    with pytest.raises(ProjectLifecycleError, match="restart is disabled in v1"):
        up_project(changed, callbacks, roots=roots, services=["api"], no_recreate=True)
    assert not any(event[0] in {"resolve", "prepare", "start"} for event in runtime.events)
    ledger = read_project_state(changed, roots)
    assert ledger is not None and "new_data" not in ledger.volumes


def test_locked_project_service_operation_and_stop_revalidate_identity(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = FakeRuntime()
    roots = _roots(tmp_path)
    up_project(project, runtime.callbacks(), roots=roots, services=["api"])

    observed = project_service_operation(
        project,
        "api",
        runtime.inspect,
        lambda run_name: ("executed", run_name),
        roots=roots,
    )
    assert observed == ("executed", "demo-api-1")

    stopped = stop_project_services(project, runtime.callbacks(), ["db", "api"], roots=roots)
    assert stopped == ("api", "db")
    assert runtime.statuses["demo-api-1"] == "stopped"
    assert runtime.statuses["demo-db-1"] == "stopped"


def test_successful_up_refreshes_dependency_order_before_down(tmp_path: Path) -> None:
    base = _project(tmp_path)
    initial = replace(
        base,
        services=MappingProxyType({"a": _service("a"), "b": _service("b")}),
        volumes=MappingProxyType({}),
    )
    changed = replace(
        initial,
        services=MappingProxyType({"a": _service("a", depends_on=("b",)), "b": _service("b")}),
    )
    runtime = FakeRuntime()
    roots = _roots(tmp_path)

    up_project(initial, runtime.callbacks(), roots=roots)
    assert read_project_state(initial, roots).order == ("a", "b")  # type: ignore[union-attr]

    up_project(changed, runtime.callbacks(), roots=roots)
    ledger = read_project_state(changed, roots)
    assert ledger is not None and ledger.order == ("b", "a")
    runtime.events.clear()
    down_project(changed, runtime.callbacks(), roots=roots)
    assert [event[1] for event in runtime.events if event[0] == "remove"] == ["demo-a-1", "demo-b-1"]

def test_valid_backend_accepts_known_backends_and_rejects_unknown() -> None:
    from palimpsest_local.project_runtime import _KNOWN_BACKENDS, _valid_backend
    assert _KNOWN_BACKENDS == {"kvm", "lima-vz", "libvirt-hvf"}
    assert _valid_backend("kvm", "backend") == "kvm"
    assert _valid_backend("lima-vz", "backend") == "lima-vz"
    assert _valid_backend("libvirt-hvf", "backend") == "libvirt-hvf"
    with pytest.raises(StateError, match=r"backend must be one of: kvm, libvirt-hvf, lima-vz"):
        _valid_backend("invalid", "backend")
