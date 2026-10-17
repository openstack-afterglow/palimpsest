"""Behavioral contracts; no VM, daemon, registry or host container is started."""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import test_packages as packages_fixture

from palimpsest_local import experimental_oci_vm as vm
from palimpsest_local import lima, platforms, runtime_dispatch
from palimpsest_local.errors import ArtifactValidationError, LifecycleError, StateError
from palimpsest_local.oci_network import OCINetworkConfig
from palimpsest_local.oci_run_request import LocalOCIRunRequest, resolve_local_oci_run_request
from palimpsest_local.state import StatePaths, init_resolved_roots


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_forced_selection_is_distinct_and_native_selection_stays_strict(architecture):
    host = platforms.HostPlatform("Darwin", "aarch64")
    assert (
        platforms.select_backend(architecture, host=host, runtime_kind="oci-root", force_architecture=True)
        == "lima-qemu-oci"
    )
    with pytest.raises(ArtifactValidationError, match="native OCI-root requires"):
        platforms.select_backend(architecture, host=host, runtime_kind="oci-root")
    with pytest.raises(ArtifactValidationError, match="only for local OCI"):
        platforms.select_backend(architecture, host=host, force_architecture=True)
    with pytest.raises(ArtifactValidationError, match="consent"):
        platforms.select_backend(architecture, host=host, runtime_kind="oci-root", requested="lima-qemu-oci")
    assert (
        platforms.select_backend("amd64", host=platforms.HostPlatform("Linux", "x86_64"), runtime_kind="oci-root")
        == "kvm"
    )


def _request(tmp_path, architecture="arm64"):
    layout = tmp_path / "layout"
    packages_fixture._new_layout(layout)
    root = packages_fixture._image(layout, architecture, {"config": {"Cmd": ["/hello", "literal;$HOME"]}})
    packages_fixture._set_roots(layout, [root])
    carrier = tmp_path / "carrier.qcow2"
    carrier.write_bytes(b"fixture-carrier-disk")
    return LocalOCIRunRequest(
        "experimental-demo",
        layout.resolve(),
        platform="linux/" + architecture,
        backend="lima-qemu-oci",
        force_architecture=True,
        vm_image=carrier.resolve(),
        vm_image_digest="sha256:" + hashlib.sha256(carrier.read_bytes()).hexdigest(),
    )


@pytest.mark.parametrize(
    "change,match",
    [
        ({"detached": True}, "foreground-only"),
        ({"root_retention": "retain"}, "disposable VM disk"),
        ({"network": OCINetworkConfig("host-only")}, "not host-only"),
        ({"network": OCINetworkConfig.resolve("nat", ["8080:80/udp"])}, "TCP on 127"),
        ({"network": OCINetworkConfig.resolve("nat", ["0.0.0.0:8080:80"])}, "TCP on 127"),
        ({"force_architecture": False}, "requires --force"),
        ({"vm_image_digest": 7}, "canonical sha256"),
    ],
)
def test_experimental_policy_rejects_unimplemented_or_unsafe_semantics(tmp_path, change, match):
    with pytest.raises(ArtifactValidationError, match=match):
        replace(_request(tmp_path), **change)


def test_native_resolution_remains_host_independent_and_force_resolves_explicit_carrier(tmp_path, monkeypatch):
    request = _request(tmp_path)
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    native = resolve_local_oci_run_request(request.source, name="native", detached=True)
    assert native.backend == "kvm" and native.detached
    forced = resolve_local_oci_run_request(
        request.source,
        name="forced",
        platform="linux/arm64",
        force_architecture=True,
        vm_image=request.vm_image,
        vm_image_digest=request.vm_image_digest,
    )
    assert forced.dispatch_key.backend == "lima-qemu-oci"
    assert forced.dispatch_key.runtime_kind == "oci-container"


def test_config_has_no_host_mounts_credentials_or_implicit_port_forwarding(tmp_path, monkeypatch):
    request = replace(_request(tmp_path), network=OCINetworkConfig.resolve("nat", ["8080:80"]))
    config = json.loads(
        lima.experimental_oci_config(
            request,
            carrier=request.vm_image,
            carrier_digest=request.vm_image_digest,
            run_id="cd962290-0671-4af9-9464-02accb82ddda",
            disk_bytes=30 * 1024**3,
        )
    )
    assert (config["vmType"], config["arch"]) == ("qemu", "aarch64")
    assert config["mounts"] == [] and config["networks"] == []
    assert config["containerd"] == {"system": False, "user": False}
    assert config["ssh"]["forwardAgent"] is False
    assert config["propagateProxyEnv"] is False
    assert config["portForwards"][0]["hostIP"] == "127.0.0.1"
    assert config["portForwards"][-1]["ignore"] is True
    monkeypatch.setenv("PALIMPSEST_TOKEN", "not-a-real-token")
    monkeypatch.setenv("DOCKER_CONFIG", "/private/registry-config")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/ssh.sock")
    monkeypatch.setattr(vm, "_lima_home", lambda directory: directory / "short-isolated-lima")
    env = vm._environment(tmp_path)
    assert not {"PALIMPSEST_TOKEN", "DOCKER_CONFIG", "SSH_AUTH_SOCK"} & env.keys()
    assert env["HOME"] == str(tmp_path / "home")
    assert env["LIMA_HOME"] == str(tmp_path / "short-isolated-lima")


def test_verified_platform_selection_exports_only_matching_graph(tmp_path):
    request = _request(tmp_path)
    amd64 = packages_fixture._image(request.source, "amd64", {"config": {"Cmd": ["/amd64"]}})
    arm64 = json.loads((request.source / "index.json").read_text())["manifests"][0]
    index = packages_fixture._blob(
        request.source,
        packages_fixture._INDEX,
        packages_fixture._json(
            {
                "schemaVersion": 2,
                "mediaType": packages_fixture._INDEX,
                "manifests": [amd64, arm64],
            }
        ),
    )
    packages_fixture._set_roots(request.source, [index])
    selected = tmp_path / "selected.tar"
    binding = vm._prepare_archive(request, selected, "palimpsest.local/fixture:arm64")
    assert binding["root_digest"] == index["digest"]
    assert binding["manifest_digest"] == arm64["digest"]
    with packages_fixture.snapshot_package(selected) as verified:
        assert verified.platforms == ({"os": "linux", "architecture": "arm64"},)
        assert amd64["digest"] not in verified.graph


def test_diffid_or_platform_mismatch_is_never_bypassed_by_force(tmp_path):
    request = _request(tmp_path)
    with pytest.raises(ArtifactValidationError, match="exactly one linux/amd64"):
        vm._prepare_archive(replace(request, platform="linux/amd64"), tmp_path / "wrong.tar", "local:wrong")
    manifest = json.loads((request.source / "index.json").read_text())["manifests"][0]
    document = json.loads((request.source / "blobs/sha256" / manifest["digest"].split(":")[1]).read_text())
    layer = request.source / "blobs/sha256" / document["layers"][0]["digest"].split(":")[1]
    layer.write_bytes(b"corrupted")
    with pytest.raises(ArtifactValidationError):
        vm._prepare_archive(request, tmp_path / "corrupt.tar", "local:corrupt")


class _LimaCommands:
    def __init__(self, request, *, cancel=False, wrong_identity=False, foreign=False, disk_info=None):
        self.request, self.cancel, self.wrong_identity, self.foreign = request, cancel, wrong_identity, foreign
        self.calls, self.instance = [], None
        self.disk_info = disk_info or {"format": "qcow2", "virtual-size": 1024**3}

    def __call__(self, directory, argv, *, timeout=None, attached=False):
        self.calls.append(argv)
        output = ""
        if argv[:2] == ["limactl", "--version"]:
            output = "limactl version 2.1.0"
        elif argv[:2] == ["qemu-img", "info"]:
            output = json.dumps(self.disk_info)
        elif argv[:2] == ["limactl", "start"]:
            config = json.loads(Path(argv[-1]).read_text())
            if self.foreign:
                config["env"]["PALIMPSEST_RUN_ID"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            self.instance = {"name": argv[-2].split("=", 1)[1], "status": "Running", "config": config}
        elif argv[:2] == ["limactl", "list"]:
            output = json.dumps([] if self.instance is None else [self.instance])
        elif argv[:2] == ["limactl", "delete"]:
            self.instance = None
        elif argv[:2] == ["limactl", "shell"]:
            if "uname" in argv:
                output = "aarch64"
            elif "sha256sum" in argv:
                output = hashlib.sha256((directory / "image.tar").read_bytes()).hexdigest() + "  copied.tar"
            elif "inspect" in argv:
                binding = json.loads((directory / "state.json").read_text())
                output = json.dumps(
                    [
                        {
                            "Id": "wrong" if self.wrong_identity else binding["config_digest"],
                            "Os": "linux",
                            "Architecture": "arm64",
                        }
                    ]
                )
            elif attached:
                if self.cancel:
                    raise vm._Cancelled(signal.SIGTERM)
                return subprocess.CompletedProcess(argv, 23, None, None)
        return subprocess.CompletedProcess(argv, 0, output, "")


def _runtime(tmp_path, monkeypatch, **options):
    request = _request(tmp_path)
    roots = init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    commands = _LimaCommands(request, **options)
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    monkeypatch.setattr(vm.shutil, "which", lambda name: "/fixture/bin/" + name)
    monkeypatch.setattr(vm, "_command", commands)
    monkeypatch.setattr(vm, "_short_home", lambda run_id: tmp_path / ("short-lima-" + run_id))
    return request, roots, commands


@pytest.mark.parametrize("cancel,exit_code", [(False, 23), (True, 143)])
def test_real_dispatch_shape_loads_verified_archive_executes_and_cleans_owned_vm(
    tmp_path, monkeypatch, cancel, exit_code
):
    request, roots, commands = _runtime(tmp_path, monkeypatch, cancel=cancel)
    result = runtime_dispatch.run_local_oci(request, roots=roots)
    assert result.exit_code == exit_code
    assert result.dispatch_key.backend == "lima-qemu-oci"
    assert commands.instance is None
    ledger = vm.inspect(roots, request.name)
    assert ledger["cleanup_complete"] is True and ledger["status"] == "removed"
    assert ledger["outcome"] == ("cancelled" if cancel else "exited")
    assert ledger["exit_code"] == exit_code
    assert not (roots.runs / request.name).exists()
    assert not (Path(result.state_path).parent / "carrier.img").exists()
    assert any("load" in command for command in commands.calls)
    launches = [command for command in commands.calls if "--pull=never" in command]
    assert len(launches) == 1 and launches[0][:2] == ["limactl", "shell"]
    assert all(command[0] != "docker" for command in commands.calls)


def test_loaded_config_identity_mismatch_prevents_execution_and_deletes_owned_vm(tmp_path, monkeypatch):
    request, roots, commands = _runtime(tmp_path, monkeypatch, wrong_identity=True)
    with pytest.raises(ArtifactValidationError, match="identity/platform"):
        vm.run(roots, request)
    assert commands.instance is None
    assert not any("--pull=never" in command for command in commands.calls)
    assert vm.inspect(roots, request.name)["outcome"] == "failed"


def test_cleanup_refuses_foreign_instance_and_retains_recovery_evidence(tmp_path, monkeypatch):
    request, roots, commands = _runtime(tmp_path, monkeypatch, foreign=True)
    with pytest.raises(LifecycleError, match="cleanup failed"):
        vm.run(roots, request)
    assert not any(command[:2] == ["limactl", "delete"] for command in commands.calls)
    assert vm.inspect(roots, request.name)["cleanup_complete"] is False
    assert (roots.state / "experimental-oci-vms" / request.name / "carrier.img").exists()


def test_missing_carrier_is_actionable_without_creating_vm_state(tmp_path, monkeypatch):
    request, roots, commands = _runtime(tmp_path, monkeypatch)
    with pytest.raises(LifecycleError, match="--vm-image"):
        vm.run(roots, replace(request, vm_image=None, vm_image_digest=None))
    assert commands.calls == []
    assert not (roots.state / "experimental-oci-vms").exists()


@pytest.mark.parametrize(
    "external",
    [
        {"backing-filename": "/external/base.qcow2"},
        {"format-specific": {"type": "qcow2", "data": {"data-file": "/external/data.raw"}}},
    ],
)
def test_carrier_external_disk_bytes_rejected_before_native_launch(tmp_path, monkeypatch, external):
    request, roots, commands = _runtime(
        tmp_path, monkeypatch, disk_info={"format": "qcow2", "virtual-size": 1024**3, **external}
    )
    with pytest.raises(ArtifactValidationError, match="self-contained"):
        vm.run(roots, request)
    assert not any(command[:2] == ["limactl", "start"] for command in commands.calls)
    assert vm.inspect(roots, request.name)["cleanup_complete"] is True


def test_carrier_integrity_and_ledger_identity_are_fail_closed(tmp_path):
    request = _request(tmp_path)
    with pytest.raises(ArtifactValidationError, match="checksum mismatch"):
        vm._snapshot_carrier(request.vm_image, tmp_path / "copy.img", "sha256:" + "0" * 64)
    assert not (tmp_path / "copy.img").exists()
    roots = init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    namespace = vm._directory(roots.state / "experimental-oci-vms")
    directory = vm._directory(namespace / request.name)
    (directory / "state.json").write_text('{"schema":"wrong"}')
    with pytest.raises(StateError, match="owner identity"):
        vm.inspect(roots, request.name)


def test_canonical_cli_force_mapping_returns_workload_status_not_detach_success(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from palimpsest_local import cli
    from palimpsest_local.runtime_types import ExperimentalOCIRunResult

    request = _request(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cli-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "cli-state"))
    monkeypatch.delenv("PALIMPSEST_STATE_HOME", raising=False)
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    monkeypatch.setattr(
        cli,
        "resolve_run_source",
        lambda *args, **kwargs: SimpleNamespace(
            is_oci=True,
            target=request.source,
            manifest_digest=None,
        ),
    )
    seen = []

    def launch(selected, *, roots):
        seen.append(selected)
        return ExperimentalOCIRunResult(
            selected.name, "4ad42118-c8a9-42ec-ae16-e53a30bba3da", "/fixture/state.json", 23
        )

    monkeypatch.setattr(runtime_dispatch, "run_local_oci", launch)
    argv = [
        "run",
        str(request.source),
        "--name",
        request.name,
        "--platform",
        "linux/arm64",
        "--force-architecture",
        "--vm-image",
        str(request.vm_image),
        "--vm-image-digest",
        request.vm_image_digest,
    ]
    assert cli.main(argv) == 23
    assert seen[0].platform == "linux/arm64" and seen[0].force_architecture
    assert seen[0].vm_image == request.vm_image and seen[0].vm_image_digest == request.vm_image_digest
    assert "not native OCI-root" in capsys.readouterr().err
    seen.clear()
    assert cli.main([*argv, "--detach"]) == 1
    assert seen == []


def test_retained_logs_and_repeated_removal_do_not_touch_other_lima_homes(tmp_path, monkeypatch):
    request, roots, commands = _runtime(tmp_path, monkeypatch)
    result = vm.run(roots, request)
    directory = Path(result.state_path).parent
    (directory / "console.log").write_bytes(b"retained workload output\n")
    assert b"".join(vm.logs(roots, request.name, follow=True)) == b"retained workload output\n"
    before = list(commands.calls)
    vm.remove(roots, request.name)
    vm.stop(roots, request.name)
    assert commands.calls == before
    assert vm.inspect(roots, request.name)["cleanup_complete"] is True


def test_force_request_cannot_enter_native_materializer(tmp_path):
    from palimpsest_local.oci_run_request import materialize_local_oci_run

    request = _request(tmp_path)
    roots = init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    with pytest.raises(ArtifactValidationError, match="does not accept experimental"):
        materialize_local_oci_run(request, roots=roots, packer_path=Path("/unused"), toolchain=None)
