from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from palimpsest_local import lima, state
from palimpsest_local.errors import StateError
from palimpsest_local.refs import BuildSpec, ImageRef, LayerRef, RunSpec, StackRef


def _spec(tmp_path: Path) -> RunSpec:
    image = tmp_path / "ubuntu-arm64.img"
    image.write_bytes(b"ubuntu-arm64")
    digest = f"sha256:{hashlib.sha256(image.read_bytes()).hexdigest()}"
    return RunSpec(name="mac-prototype", stack=StackRef(ImageRef(digest, "raw", "aarch64", "ubuntu", image), ()))


def test_lima_config_is_native_arm64_with_vznat(tmp_path: Path):
    config = lima._lima_config(_spec(tmp_path))
    assert "vmType: vz" in config
    assert "arch: aarch64" in config
    assert "mounts: []" in config
    assert "- vzNAT: true" in config
    assert "lima: vzNAT" not in config
    assert "file:///" in config


def test_lima_run_persists_guest_ip_and_ssh_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    spec = _spec(tmp_path)
    calls: list[list[str]] = []

    def fake_command(argv: list[str], *, timeout_seconds: float = 600) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["limactl", "list", "--format"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                '[{"name":"mac-prototype","status":"Running","sshLocalPort":61234,"sshConfigFile":"/tmp/lima-ssh"}]',
                "",
            )
        if argv[:3] == ["limactl", "shell", "mac-prototype"]:
            return subprocess.CompletedProcess(
                argv, 0, "2: eth0    inet 192.168.64.12/24 brd 192.168.64.255 scope global eth0\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "available", lambda: True)
    monkeypatch.setattr(lima, "_run_command", fake_command)

    record = lima.run(spec, roots=roots)

    assert record["status"] == "running"
    assert record["guest_ip"] == "192.168.64.12"
    assert record["ssh_host"] == "127.0.0.1"
    assert record["ssh_local_port"] == 61234
    assert ["limactl", "create", "--tty=false", "--name", "mac-prototype"] == calls[0][:5]
    assert ["limactl", "start", "--timeout", "600s", "mac-prototype"] == calls[1]
    assert lima.shell_command("mac-prototype", roots=roots) == ["limactl", "shell", "mac-prototype"]
    assert lima.exec_command("mac-prototype", ["uname", "-m"], roots=roots) == [
        "limactl",
        "shell",
        "mac-prototype",
        "uname",
        "-m",
    ]


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
