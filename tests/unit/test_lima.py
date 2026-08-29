from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import lima, state
from palimpsest_local.errors import LifecycleError, StateError
from palimpsest_local.refs import (
    BuildSpec,
    ImageRef,
    LayerRef,
    PortForward,
    RunSpec,
    StackRef,
    VolumeAttachment,
)
from palimpsest_local.runtime_types import ExecRequest, RuntimeCapabilityError, RuntimeOperation


def _spec(tmp_path: Path) -> RunSpec:
    image = tmp_path / "ubuntu-arm64.img"
    image.write_bytes(b"ubuntu-arm64")
    digest = f"sha256:{hashlib.sha256(image.read_bytes()).hexdigest()}"
    return RunSpec(name="mac-prototype", stack=StackRef(ImageRef(digest, "raw", "aarch64", "ubuntu", image), ()))


def _legacy_lima_lifecycle(
    roots: state.StatePaths,
    name: str,
    status: str,
) -> tuple[state.RunPaths, state.OwnerRecord]:
    rpaths = state.run_paths(roots, name)
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.console.write_text("old log\n", encoding="utf-8")
    rpaths.console.chmod(0o600)
    owner = state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths,
        status=status,
        data={"backend": "lima-vz", "layers": [], "volumes": [], "guest_ip": None},
    )
    return rpaths, owner


def test_lima_config_is_native_arm64_with_vznat(tmp_path: Path):
    config = lima._lima_config(_spec(tmp_path))
    assert "vmType: vz" in config
    assert "arch: aarch64" in config
    assert "mounts: []" in config
    assert "- vzNAT: true" in config
    assert "lima: vzNAT" not in config
    assert "file:///" in config


def test_lima_network_preflight_requires_supported_named_network(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv == ["limactl", "--version"]:
            return subprocess.CompletedProcess(argv, 0, "limactl version 2.1.4\n", "")
        if argv == ["limactl", "network", "list", "--json"]:
            return subprocess.CompletedProcess(argv, 0, '{"name":"shared"}\n', "")
        raise AssertionError(argv)

    monkeypatch.setattr(lima, "available", lambda: True)
    monkeypatch.setattr(lima, "_run_command", fake_command)

    lima.validate_network("default")
    lima.validate_network("lima:shared")
    with pytest.raises(LifecycleError, match="does not exist"):
        lima.validate_network("lima:missing")

    assert ["limactl", "network", "list", "--json"] in calls


def test_lima_network_preflight_rejects_malformed_or_duplicate_list(monkeypatch: pytest.MonkeyPatch):
    output = {'{"name":"dup"}\n{"name":"dup"}\n'}

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        if argv == ["limactl", "--version"]:
            return subprocess.CompletedProcess(argv, 0, "limactl version 2.1.4\n", "")
        return subprocess.CompletedProcess(argv, 0, next(iter(output)), "")

    monkeypatch.setattr(lima, "available", lambda: True)
    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(LifecycleError, match="duplicate"):
        lima.validate_network("lima:dup")

    output.clear()
    output.add("not-json")
    with pytest.raises(LifecycleError, match="invalid network JSON"):
        lima.validate_network("lima:dup")


def test_lima_config_wires_block_volume_loopback_port_and_environment(tmp_path: Path):
    base = _spec(tmp_path)
    spec = RunSpec(
        **{
            **base.__dict__,
            "ports": (PortForward("127.0.0.1", 18080, 8080),),
            "volumes": (VolumeAttachment("data", "/var/lib/data", backend_name="project-data", format=True),),
            "environment": (("APP_ENV", "production"),),
            "cloud_init": SimpleNamespace(
                packages=("curl",),
                write_files=(SimpleNamespace(path="/etc/demo.conf", content="mode=prod", permissions="0640"),),
                runcmd=(("systemctl", "restart", "demo.service"),),
            ),
        }
    )

    config = lima._lima_config(spec)

    assert 'name: "project-data"' in config
    assert "format: true" in config
    assert "guestPort: 8080" in config
    assert "hostPort: 18080" in config
    assert 'hostIP: "127.0.0.1"' in config
    assert "static: true" in config
    assert 'APP_ENV: "production"' in config
    assert "mount --bind /mnt/lima-project-data /var/lib/data" in config
    assert "mode: data" in config
    assert 'path: "/etc/demo.conf"' in config
    assert "overwrite: true" in config
    assert "apt-get install -y -- curl" in config
    assert "systemctl restart demo.service" in config


def test_lima_config_never_formats_external_or_existing_volume(tmp_path: Path):
    base = _spec(tmp_path)
    spec = RunSpec(
        **{
            **base.__dict__,
            "volumes": (VolumeAttachment("external_data", "/srv/data", backend_name="external-data", format=False),),
        }
    )

    config = lima._lima_config(spec)

    assert 'name: "external-data"\n    format: false\n    fsType: "ext4"' in config
    assert "format: true" not in config


def test_lima_run_persists_guest_ip_and_ssh_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    spec = _spec(tmp_path)
    calls: list[list[str]] = []
    list_calls = 0
    run_id: str | None = None
    instance_status = "Running"

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        nonlocal instance_status, list_calls, run_id
        calls.append(argv)
        if argv[:2] == ["limactl", "create"]:
            config = Path(argv[-1]).read_text(encoding="utf-8")
            match = re.search(r'PALIMPSEST_RUN_ID: "([0-9a-f-]+)"', config)
            assert match is not None
            run_id = match.group(1)
        if argv[:3] == ["limactl", "list", "--format"]:
            list_calls += 1
            if list_calls == 1:
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            instance = {
                "name": "mac-prototype",
                "status": instance_status,
                "sshLocalPort": 61234,
                "sshConfigFile": "/tmp/lima-ssh",
                "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        if argv[:2] == ["limactl", "stop"]:
            instance_status = "Stopped"
        if argv[:2] == ["limactl", "start"]:
            instance_status = "Running"
        if argv[:3] == ["limactl", "shell", "mac-prototype"]:
            return subprocess.CompletedProcess(
                argv, 0, "2: eth0    inet 192.168.64.12/24 brd 192.168.64.255 scope global eth0\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "available", lambda: True)
    monkeypatch.setattr(lima, "_run_command", fake_command)

    record = lima.run(spec, roots=roots)

    assert record["status"] == "running"
    owner = state.read_owner_record(state.run_paths(roots, spec.name))
    assert record["name"] == spec.name
    assert record["run_id"] == owner.run_id
    assert record["backend"] == "lima-vz"
    assert record["schema_version"] == 2
    assert record["runtime_kind"] == "cloud-image"
    assert record["guest_ip"] == "192.168.64.12"
    assert record["ssh_host"] == "127.0.0.1"
    assert record["ssh_local_port"] == 61234
    assert record["layer_attachment"] == {
        "delivery": "scp-copy",
        "device": "loop",
        "mount": "squashfs-ro",
    }
    assert any(call[:5] == ["limactl", "create", "--tty=false", "--name", "mac-prototype"] for call in calls)
    assert ["limactl", "start", "--timeout", "600s", "mac-prototype"] in calls
    assert lima.stop("mac-prototype", roots=roots)["status"] == "stopped"
    assert lima.start("mac-prototype", roots=roots)["status"] == "running"
    assert lima.shell_command("mac-prototype", roots=roots) == ["limactl", "shell", "mac-prototype"]
    assert lima.exec_command("mac-prototype", ["uname", "-m"], roots=roots) == [
        "limactl",
        "shell",
        "mac-prototype",
        "uname",
        "-m",
    ]


def test_lima_process_adapters_are_typed_unavailable_without_backend_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    _legacy_lima_lifecycle(roots, "owned-process", "running")
    monkeypatch.setattr(lima, "_run_command", lambda *_args, **_kwargs: pytest.fail("backend probe reached"))
    monkeypatch.setattr(
        lima, "spawn_process_session", lambda *_args, **_kwargs: pytest.fail("spawn reached"), raising=False
    )

    with pytest.raises(RuntimeCapabilityError) as exec_error:
        lima.exec_session("owned-process", ExecRequest(("uname", "-m")), roots=roots)
    with pytest.raises(RuntimeCapabilityError) as shell_error:
        lima.shell_session("owned-process", roots=roots)

    assert exec_error.value.operation is RuntimeOperation.EXEC
    assert shell_error.value.operation is RuntimeOperation.SHELL


def test_legacy_lima_start_promotes_only_after_backend_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, owner = _legacy_lima_lifecycle(roots, "legacy-lima-start", "stopped")
    live_status = "Stopped"

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        nonlocal live_status
        if argv[:3] == ["limactl", "list", "--format"]:
            instance = {
                "name": "legacy-lima-start",
                "status": live_status,
                "config": {"env": {"PALIMPSEST_RUN_ID": owner.run_id}},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        if argv[:2] == ["limactl", "start"]:
            live_status = "Running"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["limactl", "shell", "legacy-lima-start"] and "addr" in argv:
            return subprocess.CompletedProcess(argv, 0, "2: eth0 inet 192.168.64.9/24 scope global eth0\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    result = lima.start("legacy-lima-start", roots=roots)

    assert {key: result[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "lima-vz",
        "name": "legacy-lima-start",
        "run_id": owner.run_id,
        "status": "running",
    }
    assert state.read_run_state(rpaths) == result


def test_legacy_lima_start_and_stop_failures_preserve_exact_state_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    start_paths, start_owner = _legacy_lima_lifecycle(roots, "legacy-lima-start-fail", "stopped")
    stop_paths, stop_owner = _legacy_lima_lifecycle(roots, "legacy-lima-stop-fail", "running")
    before_start = start_paths.state.read_bytes()
    before_stop = stop_paths.state.read_bytes()

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["limactl", "list", "--format"]:
            requested = "legacy-lima-start-fail" if "legacy-lima-start-fail" in argv[-1] else None
            instances = [
                {
                    "name": "legacy-lima-start-fail",
                    "status": "Stopped",
                    "config": {"env": {"PALIMPSEST_RUN_ID": start_owner.run_id}},
                },
                {
                    "name": "legacy-lima-stop-fail",
                    "status": "Running",
                    "config": {"env": {"PALIMPSEST_RUN_ID": stop_owner.run_id}},
                },
            ]
            assert requested is None
            return subprocess.CompletedProcess(argv, 0, json.dumps(instances), "")
        if argv[:2] in (["limactl", "start"], ["limactl", "stop"]):
            return subprocess.CompletedProcess(argv, 1, "", "backend failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(LifecycleError, match="backend failed"):
        lima.start("legacy-lima-start-fail", roots=roots)
    with pytest.raises(LifecycleError, match="backend failed"):
        lima.stop("legacy-lima-stop-fail", roots=roots)

    assert start_paths.state.read_bytes() == before_start
    assert stop_paths.state.read_bytes() == before_stop


def test_legacy_lima_stop_success_promotes_and_reconcile_discovery_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    stop_paths, stop_owner = _legacy_lima_lifecycle(roots, "legacy-lima-stop", "running")
    discovery_paths, discovery_owner = _legacy_lima_lifecycle(roots, "legacy-lima-discovery", "stopped")
    discovery_before = discovery_paths.state.read_bytes()
    statuses = {"legacy-lima-stop": "Running", "legacy-lima-discovery": "Running"}

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["limactl", "list", "--format"]:
            instances = [
                {
                    "name": name,
                    "status": live,
                    "config": {
                        "env": {
                            "PALIMPSEST_RUN_ID": stop_owner.run_id
                            if name == "legacy-lima-stop"
                            else discovery_owner.run_id
                        }
                    },
                }
                for name, live in statuses.items()
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(instances), "")
        if argv[:2] == ["limactl", "stop"]:
            statuses[argv[-1]] = "Stopped"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    stopped = lima.stop("legacy-lima-stop", roots=roots)
    discovered = lima.reconcile_run("legacy-lima-discovery", roots=roots)

    assert stopped["schema_version"] == 2
    assert stopped["status"] == "stopped"
    assert discovered["state"]["status"] == "running"
    assert discovery_paths.state.read_bytes() == discovery_before
    assert state.read_run_state(stop_paths)["run_id"] == stop_owner.run_id


@pytest.mark.parametrize("ledger_status", ["failed", "removed"])
@pytest.mark.parametrize("operation", ["start", "stop"])
def test_lima_lifecycle_rejects_ambiguous_terminal_ledger_with_live_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_status: str,
    operation: str,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    name = f"lima-{operation}-{ledger_status}"
    rpaths, owner = _legacy_lima_lifecycle(roots, name, ledger_status)
    before = rpaths.state.read_bytes()
    backend_calls: list[list[str]] = []

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["limactl", "list", "--format"]:
            instance = {
                "name": name,
                "status": "Running",
                "config": {"env": {"PALIMPSEST_RUN_ID": owner.run_id}},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        backend_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(StateError, match="foreign or ambiguous"):
        getattr(lima, operation)(name, roots=roots)

    assert backend_calls == []
    assert rpaths.state.read_bytes() == before


def test_legacy_lima_start_rejects_live_intermediate_without_backend_or_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, owner = _legacy_lima_lifecycle(roots, "legacy-lima-starting", "stopped")
    before = rpaths.state.read_bytes()
    backend_calls: list[list[str]] = []

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["limactl", "list", "--format"]:
            instance = {
                "name": "legacy-lima-starting",
                "status": "Starting",
                "config": {"env": {"PALIMPSEST_RUN_ID": owner.run_id}},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        backend_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(LifecycleError, match="cannot be started from status 'starting'"):
        lima.start("legacy-lima-starting", roots=roots)

    assert backend_calls == []
    assert rpaths.state.read_bytes() == before


def test_lima_v2_start_records_live_intermediate_before_refusing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, owner = _legacy_lima_lifecycle(roots, "v2-lima-starting", "stopped")
    state.write_run_state(
        rpaths,
        status="stopped",
        data={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "name": rpaths.name,
            "run_id": owner.run_id,
            "layers": [],
            "volumes": [],
        },
    )

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        instance = {
            "name": rpaths.name,
            "status": "Starting",
            "config": {"env": {"PALIMPSEST_RUN_ID": owner.run_id}},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(LifecycleError, match="cannot be started from status 'starting'"):
        lima.start(rpaths.name, roots=roots)

    assert state.read_run_state(rpaths)["status"] == "starting"


def test_lima_v2_stop_records_live_running_before_backend_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, owner = _legacy_lima_lifecycle(roots, "v2-lima-stop-failure", "stopped")
    state.write_run_state(
        rpaths,
        status="stopped",
        data={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "name": rpaths.name,
            "run_id": owner.run_id,
            "layers": [],
            "volumes": [],
        },
    )

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["limactl", "list", "--format"]:
            instance = {
                "name": rpaths.name,
                "status": "Running",
                "config": {"env": {"PALIMPSEST_RUN_ID": owner.run_id}},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        return subprocess.CompletedProcess(argv, 1, "", "stop failed")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(LifecycleError, match="stop failed"):
        lima.stop(rpaths.name, roots=roots)

    assert state.read_run_state(rpaths)["status"] == "running"


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_lima_v2_missing_instance_records_removed_before_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, owner = _legacy_lima_lifecycle(roots, f"v2-lima-missing-{operation}", "stopped")
    state.write_run_state(
        rpaths,
        status="stopped",
        data={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "name": rpaths.name,
            "run_id": owner.run_id,
            "layers": [],
            "volumes": [],
        },
    )
    monkeypatch.setattr(
        lima,
        "_run_command",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "[]", ""),
    )

    with pytest.raises(StateError, match="owned Lima instance is missing"):
        getattr(lima, operation)(rpaths.name, roots=roots)

    assert state.read_run_state(rpaths)["status"] == "removed"


def test_legacy_lima_start_keyboard_interrupt_stops_owned_backend_and_preserves_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, owner = _legacy_lima_lifecycle(roots, "legacy-lima-interrupt", "stopped")
    before = rpaths.state.read_bytes()
    live_status = "Stopped"
    stop_called = False

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        nonlocal live_status, stop_called
        if argv[:3] == ["limactl", "list", "--format"]:
            instance = {
                "name": rpaths.name,
                "status": live_status,
                "config": {"env": {"PALIMPSEST_RUN_ID": owner.run_id}},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        if argv[:2] == ["limactl", "start"]:
            live_status = "Running"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["limactl", "stop"]:
            stop_called = True
            live_status = "Stopped"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["limactl", "shell", rpaths.name]:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(KeyboardInterrupt):
        lima.start(rpaths.name, roots=roots)

    assert stop_called is True
    assert live_status == "Stopped"
    assert rpaths.state.read_bytes() == before


def test_legacy_lima_plain_rm_noop_preserves_removed_state_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, _owner = _legacy_lima_lifecycle(roots, "legacy-lima-rm-noop", "removed")
    before = rpaths.state.read_bytes()
    monkeypatch.setattr(
        lima,
        "_run_command",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "[]", ""),
    )

    removed = lima.rm("legacy-lima-rm-noop", roots=roots)

    assert removed["status"] == "removed"
    assert removed.get("schema_version") is None
    assert rpaths.state.read_bytes() == before


def test_lima_rm_volumes_cleans_up_exact_owned_failed_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, owner = _legacy_lima_lifecycle(roots, "failed-lima-cleanup", "failed")
    present = True

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        nonlocal present
        if argv[:3] == ["limactl", "list", "--format"]:
            instances = []
            if present:
                instances.append(
                    {
                        "name": rpaths.name,
                        "status": "Running",
                        "config": {"env": {"PALIMPSEST_RUN_ID": owner.run_id}},
                    }
                )
            return subprocess.CompletedProcess(argv, 0, json.dumps(instances), "")
        if argv[:2] == ["limactl", "delete"]:
            present = False
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    removed = lima.rm(rpaths.name, roots=roots, volumes=True)

    assert removed["status"] == "removed"
    assert present is False
    assert not rpaths.root.exists()


def test_lima_create_failure_holds_exact_v2_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    spec = _spec(tmp_path)

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["limactl", "list", "--format"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:2] == ["limactl", "create"]:
            rpaths = state.run_paths(roots, spec.name)
            owner = state.read_owner_record(rpaths)
            creating = state.read_run_state(rpaths)
            assert {
                key: creating[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")
            } == {
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": "lima-vz",
                "name": spec.name,
                "run_id": owner.run_id,
                "status": "creating",
            }
            return subprocess.CompletedProcess(argv, 1, "", "create exploded")
        raise AssertionError(argv)

    monkeypatch.setattr(lima, "available", lambda: True)
    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(LifecycleError, match="create exploded"):
        lima.run(spec, roots=roots)

    rpaths = state.run_paths(roots, spec.name)
    owner = state.read_owner_record(rpaths)
    failed = state.read_run_state(rpaths)
    assert {key: failed[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "lima-vz",
        "name": spec.name,
        "run_id": owner.run_id,
        "status": "failed",
    }


def test_lima_pre_reservation_failure_creates_no_run_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    spec = _spec(tmp_path)
    monkeypatch.setattr(lima, "available", lambda: False)

    with pytest.raises(LifecycleError, match="native Lima VZ runs require"):
        lima.run(spec, roots=roots)

    assert not (roots.runs / spec.name).exists()


def test_lima_first_use_format_is_sealed_false_before_run_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    base = _spec(tmp_path)
    spec = RunSpec(
        **{
            **base.__dict__,
            "volumes": (VolumeAttachment("app_data", "/srv/data", backend_name="abc234def56", format=True),),
        }
    )
    calls: list[list[str]] = []
    run_id: str | None = None
    instance_exists = False
    disk_format_enabled = True

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        nonlocal run_id, instance_exists, disk_format_enabled
        calls.append(argv)
        if argv[:2] == ["limactl", "create"]:
            config = Path(argv[-1]).read_text(encoding="utf-8")
            match = re.search(r'PALIMPSEST_RUN_ID: "([0-9a-f-]+)"', config)
            assert match is not None
            run_id = match.group(1)
            instance_exists = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["limactl", "list", "--format"]:
            if not instance_exists:
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            instance = {
                "name": spec.name,
                "status": "Running",
                "sshLocalPort": 61234,
                "sshConfigFile": "/tmp/lima-ssh",
                "config": {
                    "env": {"PALIMPSEST_RUN_ID": run_id},
                    "additionalDisks": [{"name": "abc234def56", "format": disk_format_enabled}],
                },
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        if argv[:2] == ["limactl", "edit"]:
            disk_format_enabled = False
        if argv[:3] == ["limactl", "shell", spec.name]:
            return subprocess.CompletedProcess(argv, 0, "2: eth0 inet 192.168.64.12/24 scope global eth0\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "available", lambda: True)
    monkeypatch.setattr(lima, "_run_command", fake_command)

    result = lima.run(spec, roots=roots)

    assert result["status"] == "running"
    assert [call[:3] for call in calls].count(["limactl", "start", "--timeout"]) == 2
    assert ["limactl", "stop", "--force", spec.name] in calls
    edit = next(call for call in calls if call[:2] == ["limactl", "edit"])
    assert edit[2:5] == [spec.name, "--tty=false", "--set"]
    assert ".additionalDisks[0].format = false" in edit[-1]
    persisted = (state.run_paths(roots, spec.name).root / "lima.yaml").read_text(encoding="utf-8")
    assert "format: false" in persisted
    assert "format: true" not in persisted


def test_lima_mutations_reject_foreign_same_name_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "owned-run")
    owner = state.write_owner_record(rpaths)
    lima._write_state(rpaths, "running", {"name": "owned-run", "backend": "lima-vz"})
    calls: list[list[str]] = []
    foreign = {
        "name": "owned-run",
        "status": "Running",
        "config": {"env": {"PALIMPSEST_RUN_ID": "00000000-0000-0000-0000-000000000000"}},
    }

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["limactl", "list", "--format"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([foreign]), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(StateError, match="foreign"):
        lima.inspect_run("owned-run", roots=roots)
    with pytest.raises(StateError, match="foreign"):
        lima.start("owned-run", roots=roots)
    with pytest.raises(StateError, match="foreign"):
        lima.stop("owned-run", roots=roots)
    with pytest.raises(StateError, match="foreign"):
        lima.shell_command("owned-run", roots=roots)
    with pytest.raises(StateError, match="foreign"):
        lima.exec_command("owned-run", ["true"], roots=roots)
    with pytest.raises(StateError, match="foreign"):
        list(lima.logs("owned-run", roots=roots))
    with pytest.raises(StateError, match="foreign"):
        lima.rm("owned-run", roots=roots, volumes=True)

    assert owner.run_id != foreign["config"]["env"]["PALIMPSEST_RUN_ID"]
    assert not any(call[:2] in (["limactl", "stop"], ["limactl", "delete"]) for call in calls)


def test_lima_start_rejects_live_disk_format_drift_before_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "owned-run")
    owner = state.write_owner_record(rpaths)
    lima._write_state(
        rpaths,
        "stopped",
        {
            "name": "owned-run",
            "backend": "lima-vz",
            "volumes": [
                {
                    "name": "data",
                    "backend_name": "abc234def56",
                    "mount_path": "/srv/data",
                    "filesystem": "ext4",
                    "read_only": False,
                }
            ],
        },
    )
    calls: list[list[str]] = []
    instance = {
        "name": "owned-run",
        "status": "Stopped",
        "config": {
            "env": {"PALIMPSEST_RUN_ID": owner.run_id},
            "additionalDisks": [{"name": "abc234def56", "format": True}],
        },
    }

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["limactl", "list", "--format"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(StateError, match="not safely sealed with format:false"):
        lima.start("owned-run", roots=roots)
    assert not any(call[:2] == ["limactl", "start"] for call in calls)


def test_lima_run_never_deletes_a_preexisting_foreign_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    calls: list[list[str]] = []

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            '[{"name":"mac-prototype","status":"Running","sshLocalPort":61234,"sshConfigFile":"/tmp/foreign"}]',
            "",
        )

    monkeypatch.setattr(lima, "available", lambda: True)
    monkeypatch.setattr(lima, "_run_command", fake_command)

    with pytest.raises(StateError, match="not owned"):
        lima.run(_spec(tmp_path), roots=roots)
    assert not any(call[:3] == ["limactl", "delete", "--force"] for call in calls)


def test_lima_build_recovers_fixed_worker_squashfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    run_spec = _spec(tmp_path)
    recipe = tmp_path / "Palimpsestfile"
    recipe.write_text(f"FROM {run_spec.stack.base.digest}\nRUN true\n", encoding="utf-8")
    build_spec = BuildSpec(
        base=run_spec.stack.base,
        parent_layers=(),
        recipe=recipe,
        network="none",
        output_name="mac-smoke",
    )
    output = b"hsqs" + b"\0" * 128
    output_digest = hashlib.sha256(output).hexdigest()
    started: list[str] = []

    monkeypatch.setattr(lima, "available", lambda: True)
    monkeypatch.setattr(lima, "run", lambda spec, **_kwargs: started.append(spec.name) or {"status": "running"})
    monkeypatch.setattr(lima, "rm", lambda name, **_kwargs: started.remove(name) or {"status": "removed"})

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["limactl", "copy", "--backend=scp"] and argv[3].startswith("builder-"):
            target = Path(argv[4])
            if argv[3].endswith("result.json"):
                target.write_text(f'{{"sha256":"{output_digest}","size":132,"status":"ok"}}\n', encoding="utf-8")
            else:
                target.write_bytes(output)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    record = lima.build_layer(build_spec, roots=roots)

    assert record["status"] == "success"
    assert record["output_digest"] == f"sha256:{output_digest}"
    assert not started


def test_lima_instance_info_accepts_json_lines(monkeypatch: pytest.MonkeyPatch):
    raw = '{"name":"other","status":"Stopped"}\n{"name":"wanted","status":"Running","sshLocalPort":1234}\n'
    monkeypatch.setattr(
        lima,
        "_run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["limactl"], 0, raw, ""),
    )

    assert lima._instance_info("wanted")["sshLocalPort"] == 1234


def test_lima_attaches_layers_as_readable_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layer_path = tmp_path / "layer.squashfs"
    layer_path.write_bytes(b"hsqs")
    layer = LayerRef(
        digest=f"sha256:{hashlib.sha256(layer_path.read_bytes()).hexdigest()}",
        media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
        local_path=layer_path,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        lima,
        "_run_command",
        lambda argv, **_kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    console = tmp_path / "console.log"
    console.write_text("", encoding="utf-8")

    lima._attach_layers("mac-prototype", (layer,), console_log=console)

    overlay = next(
        argv
        for argv in calls
        if argv[:6] == ["limactl", "shell", "mac-prototype", "sudo", "mount", "-t"] and "overlay" in argv
    )
    assert "-o" in overlay
    assert any(argument.startswith("lowerdir=/mnt/palimpsest/lower0,") for argument in overlay)
    assert any(argv[-3:] == ["chmod", "0755", "/opt/layers/merged"] for argv in calls)


def test_lima_run_explains_how_to_free_retained_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "mac-prototype")
    rpaths.root.mkdir(parents=True, mode=0o700)
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="removed", data={"backend": "lima-vz"})
    monkeypatch.setattr(lima, "available", lambda: True)
    with pytest.raises(StateError, match=r"free it with: palimpsest rm mac-prototype --volumes"):
        lima.run(_spec(tmp_path), roots=roots)


def test_lima_logs_read_current_boot_guest_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "mac-prototype")
    rpaths.root.mkdir(parents=True, mode=0o700)
    owner = state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"backend": "lima-vz"})
    calls: list[list[str]] = []

    def fake_command(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["limactl", "list", "--format"]:
            instance = {
                "name": "mac-prototype",
                "status": "Running",
                "config": {"env": {"PALIMPSEST_RUN_ID": owner.run_id}},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([instance]), "")
        return subprocess.CompletedProcess(argv, 0, "first\nsecond\n", "")

    monkeypatch.setattr(lima, "_run_command", fake_command)

    assert list(lima.logs("mac-prototype", roots=roots)) == ["first\n", "second\n"]
    assert calls[-1] == [
        "limactl",
        "shell",
        "mac-prototype",
        "journalctl",
        "-b",
        "--no-pager",
        "-o",
        "cat",
    ]
    assert calls[0] == ["limactl", "list", "--format", "json"]


def test_lima_stopped_logs_use_local_console_and_cannot_follow(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "mac-prototype")
    rpaths.root.mkdir(parents=True, mode=0o700)
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="stopped", data={"backend": "lima-vz"})
    rpaths.console.write_text("provisioned\n", encoding="utf-8")

    assert list(lima.logs("mac-prototype", roots=roots)) == ["provisioned\n"]
    with pytest.raises(LifecycleError, match="cannot follow"):
        list(lima.logs("mac-prototype", roots=roots, follow=True))


def test_lima_empty_stack_still_creates_merged_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        lima,
        "_run_command",
        lambda argv, **_kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    console = tmp_path / "console.log"
    console.write_text("", encoding="utf-8")

    lima._attach_layers("mac-prototype", (), console_log=console)

    assert any("/opt/layers/merged" in argv for argv in calls)
    assert not any(
        argv[:7] == ["limactl", "shell", "mac-prototype", "sudo", "mount", "-t", "overlay"] for argv in calls
    )
