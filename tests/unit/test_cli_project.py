"""CLI contracts for declarative ``palimpsest.yml`` projects."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import cli, state
from palimpsest_local import project as project_model
from palimpsest_local.project_runtime import ManagedVolume, ProjectCallbacks, read_project_state
from palimpsest_local.refs import HostDirectoryShare
from palimpsest_local.runtime_types import (
    CloudImageInspectDetail,
    DispatchKey,
    ExistingRunRecord,
    ExpectedRunIdentity,
    InspectBase,
    InspectLifecycle,
    InspectPort,
    InspectRecord,
    InspectSshEndpoint,
    RuntimeBackend,
    RuntimeKind,
)

_IMAGE = "sha256:" + "a" * 64


def _typed_inspect(status: str, ports: tuple[InspectPort, ...] = ()) -> InspectRecord:
    return InspectRecord(
        schema_version=1,
        record=ExistingRunRecord(
            "demo-api-1",
            "00000000-0000-0000-0000-000000000001",
            2,
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
        ),
        lifecycle=InspectLifecycle(status, 0, None, None),
        detail=CloudImageInspectDetail(
            InspectBase(None, None, None),
            (),
            None,
            None,
            None,
            ports,
            (),
            InspectSshEndpoint(None, 22),
            None,
        ),
    )


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def _write_project(root: Path, *, name: str = "demo") -> Path:
    path = root / "palimpsest.yml"
    path.write_text(
        f"""version: "1"
name: {name}
services:
  api:
    image: {_IMAGE}
    memory: 1GiB
    vcpus: 2
    ports: ["18080:8080"]
""",
        encoding="utf-8",
    )
    return path


def test_compose_config_discovers_default_file_and_lists_services(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_project(tmp_path)

    assert cli.main(["compose", "--project-directory", str(tmp_path), "config", "--services"]) == 0

    assert capsys.readouterr().out == "api\n"


def test_compose_explicit_file_uses_cwd_while_project_directory_controls_resources_and_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = tmp_path / "invocation"
    project_root = tmp_path / "chosen-root"
    (invocation / "config").mkdir(parents=True)
    (project_root / "config").mkdir(parents=True)
    (project_root / ".env").write_text(f"PROJECT_IMAGE={_IMAGE}\n", encoding="utf-8")
    (project_root / "runtime.env").write_text("PUBLIC=from-project-root\n", encoding="utf-8")
    (invocation / "config" / "palimpsest.yml").write_text(
        """services:
  selected:
    image: ${PROJECT_IMAGE}
    env_file: runtime.env
""",
        encoding="utf-8",
    )
    (project_root / "config" / "palimpsest.yml").write_text(
        f"services:\n  wrong:\n    image: {_IMAGE}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(invocation)
    monkeypatch.delenv("PROJECT_IMAGE", raising=False)
    monkeypatch.delenv("PALIMPSEST_PROJECT_NAME", raising=False)
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    args = cli.build_parser().parse_args(
        [
            "compose",
            "--project-directory",
            str(project_root),
            "-f",
            "config/palimpsest.yml",
            "config",
        ]
    )

    project, environment = cli._load_compose_project(args)

    assert project.source == (invocation / "config" / "palimpsest.yml").resolve()
    assert project.root == project_root.resolve()
    assert project.name == "chosen-root"
    assert tuple(project.services) == ("selected",)
    assert project.services["selected"].env_files[0].path == (project_root / "runtime.env").resolve()
    assert environment["PROJECT_IMAGE"] == _IMAGE


def test_compose_project_name_cli_precedes_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(tmp_path)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "from-environment")
    args = cli.build_parser().parse_args(
        ["compose", "--project-directory", str(tmp_path), "--project-name", "from-cli", "config"]
    )

    project, _environment = cli._load_compose_project(args)

    assert project.name == "from-cli"


def test_compose_up_passes_selection_and_recreate_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_project(tmp_path)
    callbacks = object()
    calls: list[object] = []
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *args: callbacks)

    def fake_up(project, received_callbacks, **kwargs):
        calls.append((project.name, received_callbacks, kwargs))
        return SimpleNamespace(actions=(SimpleNamespace(service="api", action="create"),))

    monkeypatch.setattr(cli, "up_project", fake_up)

    result = cli.main(
        [
            "compose",
            "--project-directory",
            str(tmp_path),
            "up",
            "--force-recreate",
            "api",
        ]
    )

    assert result == 0
    assert calls[0][0:2] == ("demo", callbacks)
    assert calls[0][2]["services"] == ["api"]
    assert calls[0][2]["force_recreate"] is True
    assert capsys.readouterr().out == "api\tcreate\n"


def test_compose_down_preserves_volumes_unless_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *args: object())
    calls: list[bool] = []

    def fake_down(_project, _callbacks, **kwargs):
        calls.append(kwargs["volumes"])
        return SimpleNamespace(removed_services=(), removed_volumes=())

    monkeypatch.setattr(cli, "down_project", fake_down)

    assert cli.main(["compose", "--project-directory", str(tmp_path), "down"]) == 0
    assert cli.main(["compose", "--project-directory", str(tmp_path), "down", "--volumes"]) == 0
    assert calls == [False, True]


@pytest.mark.parametrize("command", [["stop"], ["down"], ["down", "--volumes"]])
@pytest.mark.parametrize("source_kind", ["absent", "symlink", "file"])
def test_public_compose_teardown_ignores_vanished_source_and_only_deletes_owned_volumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    source_kind: str,
) -> None:
    source = tmp_path / "host-data"
    source.mkdir()
    project_file = tmp_path / "palimpsest.yml"
    project_file.write_text(
        f"""name: demo
volumes:
  data: {{size: 1GiB}}
  foreign: {{external: true}}
services:
  api:
    image: {_IMAGE}
    volumes:
      - data:/var/lib/data
      - type: bind
        source: host-data
        target: /srv/shared
""",
        encoding="utf-8",
    )
    owned_volume = tmp_path / "owned-volume.raw"
    foreign_volume = tmp_path / "foreign-volume.raw"
    foreign_volume.write_bytes(b"foreign data")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_bytes(b"external data")
    runs: dict[str, dict[str, object]] = {}
    effects: list[tuple[str, str]] = []
    loaded: list[object] = []

    def prepare(items):
        owned_volume.write_bytes(b"owned data")
        return (ManagedVolume("data", "kvm", 1024**3),)

    def start(item, *, expected_identity=None):
        name = item.plan.run_name
        runs[name] = {"owner": {"run_id": str(uuid.uuid4())}, "state": {"status": "running", "backend": "kvm"}}

    def stop(name, *, expected_identity=None):
        assert expected_identity.run_id == runs[name]["owner"]["run_id"]
        effects.append(("stop", name))
        runs[name]["state"]["status"] = "stopped"

    def remove(name, *, expected_identity=None):
        assert expected_identity.run_id == runs[name]["owner"]["run_id"]
        effects.append(("remove", name))
        del runs[name]

    def remove_volume(project_name, volume_name, backend, size_bytes):
        assert (project_name, volume_name, backend, size_bytes) == ("demo", "data", "kvm", 1024**3)
        effects.append(("remove-volume", volume_name))
        owned_volume.unlink()

    callbacks = ProjectCallbacks(
        inspect=runs.get,
        resolve=lambda *_args: None,
        start=start,
        stop=stop,
        remove=remove,
        prepare=prepare,
        remove_volume=remove_volume,
    )

    def build_callbacks(project, *_args):
        loaded.append(project)
        return callbacks

    monkeypatch.setattr(cli, "_compose_callbacks", build_callbacks)
    base = ["compose", "--project-directory", str(tmp_path)]
    assert cli.main([*base, "up"]) == 0
    source.rmdir()
    if source_kind == "symlink":
        source.symlink_to(outside, target_is_directory=True)
    elif source_kind == "file":
        source.write_bytes(b"not a directory")
    monkeypatch.setattr(HostDirectoryShare, "validate_source", lambda *_args: pytest.fail("cleanup inspected source"))
    monkeypatch.setattr(
        project_model, "_safe_project_path", lambda *_args, **_kwargs: pytest.fail("cleanup followed source")
    )

    assert cli.main([*base, *command]) == 0
    roots = state.resolve_roots()
    ledger = read_project_state(loaded[-1], roots)
    assert ledger is not None
    if command == ["stop"]:
        assert effects == [("stop", "demo-api-1")]
        assert set(ledger.services) == {"api"}
        assert runs["demo-api-1"]["state"]["status"] == "stopped"
    else:
        assert effects[:2] == [("stop", "demo-api-1"), ("remove", "demo-api-1")]
        assert not ledger.services
        assert not runs
    if "--volumes" in command:
        assert effects[-1] == ("remove-volume", "data")
        assert not owned_volume.exists()
        assert not ledger.volumes
    else:
        assert owned_volume.read_bytes() == b"owned data"
        assert set(ledger.volumes) == {"data"}
    assert foreign_volume.read_bytes() == b"foreign data"
    assert sentinel.read_bytes() == b"external data"
    if source_kind == "absent":
        assert not source.exists()
    elif source_kind == "symlink":
        assert source.is_symlink()
    else:
        assert source.read_bytes() == b"not a directory"


def test_public_compose_config_still_validates_live_bind_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "palimpsest.yml").write_text(
        f"""services:
  api:
    image: {_IMAGE}
    volumes:
      - type: bind
        source: missing
        target: /srv/shared
""",
        encoding="utf-8",
    )
    assert cli.main(["compose", "--project-directory", str(tmp_path), "config", "--quiet"]) == 1
    assert "cannot access path" in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()


def test_compose_ps_json_uses_logical_service_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_project(tmp_path)
    callbacks = SimpleNamespace(inspect=lambda _name: None)
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *args: callbacks)
    monkeypatch.setattr(
        cli,
        "project_ps",
        lambda *_args, **_kwargs: (SimpleNamespace(service="api", run_name="demo-api-1", status="running"),),
    )

    assert cli.main(["compose", "--project-directory", str(tmp_path), "ps", "--format", "json"]) == 0

    output = capsys.readouterr().out
    assert '"service": "api"' in output
    assert '"name": "demo-api-1"' in output


def test_compose_follow_rejects_multiple_services_before_reading_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_project(tmp_path)
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *args: object())
    monkeypatch.setattr(cli, "project_log_targets", lambda *_args, **_kwargs: (("api", "one"), ("db", "two")))

    assert cli.main(["compose", "--project-directory", str(tmp_path), "logs", "--follow"]) == 1
    assert "requires exactly one service" in capsys.readouterr().err


def test_compose_exec_preserves_argv_and_never_uses_a_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *args: SimpleNamespace(inspect=lambda _name: None))
    monkeypatch.setattr(
        cli,
        "project_service_operation",
        lambda _project, _service, _inspect, operation, **_kwargs: operation(
            "demo-api-1",
            expected_identity=ExpectedRunIdentity(
                "demo-api-1",
                "00000000-0000-0000-0000-000000000001",
                DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            ),
        ),
    )
    session = object()
    calls: list[tuple[str, list[str], ExpectedRunIdentity | None]] = []

    def fake_exec(name, command, *, roots, expected_identity):
        del roots
        calls.append((name, command, expected_identity))
        return session

    monkeypatch.setattr(cli.runtime_dispatch, "exec", fake_exec)
    monkeypatch.setattr(
        cli,
        "_run_process_session",
        lambda candidate, *, interactive: 17 if candidate is session and not interactive else pytest.fail(),
    )

    result = cli.main(
        ["compose", "--project-directory", str(tmp_path), "exec", "api", "--", "printf", "%s", "hello world"]
    )

    assert result == 17
    assert len(calls) == 1
    assert calls[0][0:2] == ("demo-api-1", ["printf", "%s", "hello world"])
    assert isinstance(calls[0][2], ExpectedRunIdentity)


def test_compose_port_prints_owner_verified_applied_mapping_instead_of_current_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_project(tmp_path)
    inspected = _typed_inspect("running", (InspectPort("127.0.0.1", 19090, 8080, "tcp"),))
    callbacks = SimpleNamespace(inspect=lambda _name: inspected)
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *args: callbacks)

    def fake_managed_run_name(_project, _service, *, roots, inspect):
        assert roots is not None
        assert inspect("demo-api-1") is inspected
        return "demo-api-1"

    monkeypatch.setattr(cli, "managed_run_name", fake_managed_run_name)

    assert cli.main(["compose", "--project-directory", str(tmp_path), "port", "api", "8080"]) == 0
    assert capsys.readouterr().out == "127.0.0.1:19090\n"


def test_compose_port_rejects_malformed_applied_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_project(tmp_path)
    callbacks = SimpleNamespace(inspect=lambda _name: {"state": {"status": "running", "ports": "18080:8080"}})
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *args: callbacks)

    def fake_managed_run_name(_project, _service, *, roots, inspect):
        inspect("demo-api-1")
        return "demo-api-1"

    monkeypatch.setattr(cli, "managed_run_name", fake_managed_run_name)

    assert cli.main(["compose", "--project-directory", str(tmp_path), "port", "api", "8080"]) == 1
    assert "typed inspect record" in capsys.readouterr().err


def test_compose_port_rejects_removed_runtime_instead_of_falling_back_to_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_project(tmp_path)
    callbacks = SimpleNamespace(
        inspect=lambda _name: _typed_inspect("removed", (InspectPort("127.0.0.1", 18080, 8080, "tcp"),))
    )
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *args: callbacks)

    def fake_managed_run_name(_project, _service, *, roots, inspect):
        inspect("demo-api-1")
        return "demo-api-1"

    monkeypatch.setattr(cli, "managed_run_name", fake_managed_run_name)

    assert cli.main(["compose", "--project-directory", str(tmp_path), "port", "api", "8080"]) == 1
    assert "has been removed" in capsys.readouterr().err


def test_compose_relative_env_file_is_resolved_from_cwd_before_project_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invocation = tmp_path / "invocation"
    project_root = tmp_path / "project"
    invocation.mkdir()
    project_root.mkdir()
    _write_project(project_root)
    (invocation / "selected.env").write_text("PUBLIC=from-cwd\n", encoding="utf-8")
    (project_root / "selected.env").write_text("PUBLIC=wrong-project-file\n", encoding="utf-8")
    monkeypatch.chdir(invocation)

    result = cli.main(
        [
            "compose",
            "--project-directory",
            str(project_root),
            "--env-file",
            "selected.env",
            "config",
            "--quiet",
        ]
    )

    assert result == 1
    assert "must stay inside" in capsys.readouterr().err


def test_compose_env_file_must_remain_inside_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_project(tmp_path)
    outside = tmp_path.parent / "outside.env"
    outside.write_text("VALUE=outside\n", encoding="utf-8")

    result = cli.main(
        [
            "compose",
            "--project-directory",
            str(tmp_path),
            "--env-file",
            str(outside),
            "config",
        ]
    )

    assert result == 1
    assert "must stay inside" in capsys.readouterr().err


def test_runtime_bundle_validation_has_no_host_lima_availability_heuristic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    calls: list[Path] = []
    monkeypatch.setattr(
        cli,
        "verify_layout_dir",
        lambda path: calls.append(path) or SimpleNamespace(manifests=()),
    )

    with pytest.raises(cli.PalimpsestError, match="exactly one selectable manifest"):
        cli._resolve_runtime_stack(SimpleNamespace(), bundle, (), None)
    assert calls == [bundle]
