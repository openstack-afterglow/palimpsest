"""CLI integration contracts for Docker-compatible registry commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from palimpsest_local import cli, registry
from palimpsest_local.registry import RegistryConfig, RegistryProfile


@pytest.fixture(autouse=True)
def _isolated_cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker-config"))
    monkeypatch.delenv("PALIMPSEST_REGISTRY", raising=False)


def _private_registry_config() -> RegistryConfig:
    private = RegistryProfile(
        alias="corp",
        endpoint="registry.example.com:5000",
        namespace="team",
        cache_from=("type=registry,ref=registry.example.com:5000/cache/profile-from",),
        cache_to=("type=registry,ref=registry.example.com:5000/cache/profile-to,mode=max",),
    )
    return registry.use_profile(registry.add_profile(registry.default_registry_config(), private), "corp")


@pytest.fixture
def docker_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RegistryConfig, Path, list[list[str]]]:
    config = _private_registry_config()
    docker_config = tmp_path / "docker-config"
    calls: list[list[str]] = []

    monkeypatch.setattr(cli, "load_registry_config", lambda _roots: config)
    monkeypatch.setattr(cli, "resolve_docker_config_dir", lambda: docker_config)

    def fake_passthrough(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli, "run_docker_passthrough", fake_passthrough)
    return config, docker_config, calls


@pytest.mark.parametrize(
    ("argv", "expected_tail"),
    [
        (
            ["login", "--registry", "corp", "--username", "alice", "--password-stdin"],
            ["login", "--username", "alice", "--password-stdin", "registry.example.com:5000"],
        ),
        (
            ["logout", "--registry", "corp"],
            ["logout", "registry.example.com:5000"],
        ),
        (
            ["pull", "worker:v1", "--registry", "corp", "--platform", "linux/arm64", "-q"],
            [
                "pull",
                "--platform",
                "linux/arm64",
                "--quiet",
                "registry.example.com:5000/team/worker:v1",
            ],
        ),
        (
            ["push", "worker", "--registry", "corp", "--all-tags"],
            ["push", "--all-tags", "registry.example.com:5000/team/worker"],
        ),
        (
            ["tag", "local-worker:v1", "worker:v2", "--registry", "corp"],
            ["tag", "local-worker:v1", "registry.example.com:5000/team/worker:v2"],
        ),
        # Unconfigured authorities remain ordinary Docker/OCI; only configured profiles can be native.
        (
            ["login", "ghcr.io", "--username", "alice", "--password-stdin"],
            ["login", "--username", "alice", "--password-stdin", "ghcr.io"],
        ),
        (["pull", "ghcr.io/acme/api:v1"], ["pull", "ghcr.io/acme/api:v1"]),
        (["push", "ghcr.io/acme/api:v1"], ["push", "ghcr.io/acme/api:v1"]),
        (
            [
                "images",
                "worker",
                "--all",
                "--digests",
                "--filter",
                "dangling=false",
                "--filter",
                "label=stage=runtime",
                "--format",
                "{{.Repository}}",
                "--no-trunc",
                "--quiet",
            ],
            [
                "images",
                "--all",
                "--digests",
                "--quiet",
                "--no-trunc",
                "--filter",
                "dangling=false",
                "--filter",
                "label=stage=runtime",
                "--format",
                "{{.Repository}}",
                "worker",
            ],
        ),
        (
            [
                "image",
                "inspect",
                "worker:v1",
                "worker:v2",
                "--registry",
                "corp",
                "--platform",
                "linux/amd64",
                "--format",
                "{{.Id}}",
            ],
            [
                "image",
                "inspect",
                "--platform",
                "linux/amd64",
                "--format",
                "{{.Id}}",
                "registry.example.com:5000/team/worker:v1",
                "registry.example.com:5000/team/worker:v2",
            ],
        ),
    ],
)
def test_registry_and_docker_image_commands_dispatch_exact_argv(
    argv: list[str],
    expected_tail: list[str],
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]],
) -> None:
    _config, docker_config, calls = docker_dispatch

    assert cli.main(argv) == 0
    assert calls == [["docker", "--config", str(docker_config.resolve()), *expected_tail]]


@pytest.mark.parametrize(
    ("argv", "expected_tail"),
    [
        (
            ["history", "worker:v1", "--format", "{{.ID}}", "--no-trunc", "-q"],
            ["image", "history", "--no-trunc", "--quiet", "--format", "{{.ID}}", "worker:v1"],
        ),
        (
            ["image", "history", "worker:v1", "--format", "{{.ID}}", "--no-trunc", "-q"],
            ["image", "history", "--no-trunc", "--quiet", "--format", "{{.ID}}", "worker:v1"],
        ),
        (
            ["rmi", "worker:v1", "worker:v2", "--force", "--no-prune"],
            ["image", "rm", "--force", "--no-prune", "worker:v1", "worker:v2"],
        ),
        (
            ["image", "rm", "worker:v1", "worker:v2", "--force", "--no-prune"],
            ["image", "rm", "--force", "--no-prune", "worker:v1", "worker:v2"],
        ),
        (
            ["save", "worker:v1", "worker:v2"],
            ["image", "save", "worker:v1", "worker:v2"],
        ),
        (
            ["image", "save", "worker:v1", "worker:v2"],
            ["image", "save", "worker:v1", "worker:v2"],
        ),
        (
            ["load", "--platform", "linux/arm64", "--quiet"],
            ["image", "load", "--platform", "linux/arm64", "--quiet"],
        ),
        (
            ["image", "load", "--platform", "linux/arm64", "--quiet"],
            ["image", "load", "--platform", "linux/arm64", "--quiet"],
        ),
    ],
)
def test_top_level_and_image_aliases_dispatch_the_same_docker_operations(
    argv: list[str],
    expected_tail: list[str],
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]],
) -> None:
    _config, docker_config, calls = docker_dispatch

    assert cli.main(argv) == 0
    assert calls == [["docker", "--config", str(docker_config.resolve()), *expected_tail]]


@pytest.mark.parametrize(
    ("prefix", "path_flag"),
    [(["save", "worker:v1"], "--output"), (["image", "save", "worker:v1"], "--output")],
)
def test_save_output_path_is_forwarded_from_both_command_forms(
    prefix: list[str],
    path_flag: str,
    tmp_path: Path,
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]],
) -> None:
    _config, docker_config, calls = docker_dispatch
    archive = tmp_path / "images.tar"

    assert cli.main([*prefix, "--output", str(archive)]) == 0
    assert calls == [
        [
            "docker",
            "--config",
            str(docker_config.resolve()),
            "image",
            "save",
            path_flag,
            str(archive.resolve()),
            "worker:v1",
        ]
    ]


@pytest.mark.parametrize("prefix", [["load"], ["image", "load"]])
def test_load_input_path_is_forwarded_from_both_command_forms(
    prefix: list[str],
    tmp_path: Path,
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]],
) -> None:
    _config, docker_config, calls = docker_dispatch
    archive = tmp_path / "images.tar"

    assert cli.main([*prefix, "--input", str(archive)]) == 0
    assert calls == [
        [
            "docker",
            "--config",
            str(docker_config.resolve()),
            "image",
            "load",
            "--input",
            str(archive.resolve()),
        ]
    ]


def test_generic_docker_passthrough_uses_existing_config_and_preserves_exit_status(
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, docker_config, calls = docker_dispatch

    def fake_failure(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 17)

    monkeypatch.setattr(cli, "run_docker_passthrough", fake_failure)

    assert cli.main(["docker", "ps", "--format", "{{.ID}}"]) == 17
    assert calls == [["docker", "--config", str(docker_config.resolve()), "ps", "--format", "{{.ID}}"]]


def test_generic_docker_passthrough_rejects_login_password_in_argv(
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]], capsys: pytest.CaptureFixture[str]
) -> None:
    _config, _docker_config, calls = docker_dispatch

    assert cli.main(["docker", "login", "-p", "secret", "registry.example.com"]) == 1
    assert calls == []
    assert "password-stdin" in capsys.readouterr().err


@pytest.mark.parametrize("password_flag", ["-psecret", "-p=secret", "--password=secret"])
def test_generic_docker_passthrough_rejects_attached_login_password(
    password_flag: str,
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]],
) -> None:
    _config, _docker_config, calls = docker_dispatch

    assert cli.main(["docker", "login", password_flag, "registry.example.com"]) == 1
    assert calls == []


def test_generic_docker_passthrough_allows_config_argument_after_subcommand(
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]],
) -> None:
    _config, docker_config, calls = docker_dispatch

    assert cli.main(["docker", "run", "app", "program", "--config=/etc/app"]) == 0
    assert calls == [
        [
            "docker",
            "--config",
            str(docker_config.resolve()),
            "run",
            "app",
            "program",
            "--config=/etc/app",
        ]
    ]


@pytest.mark.parametrize("operation", ["pull", "push"])
@pytest.mark.parametrize("reference", ["worker:staging", "worker@sha256:" + "a" * 64])
def test_all_tags_rejects_tagged_or_digest_reference_before_docker(
    operation: str,
    reference: str,
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]],
) -> None:
    _config, _docker_config, calls = docker_dispatch

    assert cli.main([operation, reference, "--all-tags"]) == 1
    assert calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["images"],
        ["history", "worker:v1"],
        ["rmi", "worker:v1"],
        ["save", "worker:v1"],
        ["load"],
    ],
)
def test_local_docker_image_commands_do_not_load_registry_profiles(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def forbidden_load(_roots: object) -> RegistryConfig:
        raise AssertionError("local Docker image command loaded registries.toml")

    monkeypatch.setattr(cli, "load_registry_config", forbidden_load)
    monkeypatch.setattr(
        cli,
        "run_docker_passthrough",
        lambda command: calls.append(list(command)) or subprocess.CompletedProcess(command, 0),
    )

    assert cli.main(argv) == 0
    assert len(calls) == 1


def test_cloud_image_and_vm_commands_remain_distinct_from_docker_commands() -> None:
    parser = cli.build_parser()
    digest = "sha256:" + "a" * 64

    cloud_pull = parser.parse_args(["image", "pull", digest])
    docker_pull = parser.parse_args(["pull", "worker:v1"])
    vm_inspect = parser.parse_args(["inspect", "demo-vm"])
    docker_inspect = parser.parse_args(["image", "inspect", "worker:v1"])
    vm_run = parser.parse_args(["run", digest, "--name", "demo-vm"])

    assert (cloud_pull.operation, cloud_pull.image_operation, cloud_pull.digest) == ("image", "pull", digest)
    assert (docker_pull.operation, docker_pull.reference) == ("pull", "worker:v1")
    assert (vm_inspect.operation, vm_inspect.name) == ("inspect", "demo-vm")
    assert (docker_inspect.operation, docker_inspect.image_operation) == ("image", "inspect")
    assert (vm_run.operation, vm_run.image_or_bundle, vm_run.name) == ("run", digest, "demo-vm")


@pytest.mark.parametrize("image_id", ["0e5574283393", "sha256:" + "a" * 64])
def test_image_inspect_preserves_local_image_ids(
    image_id: str,
    docker_dispatch: tuple[RegistryConfig, Path, list[list[str]]],
) -> None:
    _config, docker_config, calls = docker_dispatch

    assert cli.main(["image", "inspect", image_id, "--registry", "corp"]) == 0
    assert calls == [["docker", "--config", str(docker_config.resolve()), "image", "inspect", image_id]]


def test_image_inspect_by_id_does_not_load_registry_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def forbidden_load(_roots: object) -> RegistryConfig:
        raise AssertionError("image ID inspection loaded registries.toml")

    monkeypatch.setattr(cli, "load_registry_config", forbidden_load)
    monkeypatch.setattr(
        cli,
        "run_docker_passthrough",
        lambda command: calls.append(list(command)) or subprocess.CompletedProcess(command, 0),
    )

    assert cli.main(["image", "inspect", "0e5574283393"]) == 0
    assert calls[0][-3:] == ["image", "inspect", "0e5574283393"]


def test_registry_profile_cli_forwards_namespace_and_cache_options(tmp_path: Path) -> None:
    cache_from = "type=registry,ref=registry.example.com:5000/cache/from"
    cache_to = "type=registry,ref=registry.example.com:5000/cache/to,mode=max"

    assert (
        cli.main(
            [
                "registry",
                "add",
                "corp",
                "registry.example.com:5000",
                "--namespace",
                "team",
                "--cache-from",
                cache_from,
                "--cache-to",
                cache_to,
                "--default",
            ]
        )
        == 0
    )

    config = registry.load_registry_config(cli.init_roots())
    assert config.default == "corp"
    assert config.registries["corp"] == RegistryProfile(
        alias="corp",
        endpoint="registry.example.com:5000",
        namespace="team",
        cache_from=(cache_from,),
        cache_to=(cache_to,),
    )
    assert registry.registry_config_path(cli.init_roots()).is_relative_to(tmp_path)


def _native_profile() -> RegistryProfile:
    return RegistryProfile(
        alias="cloud",
        endpoint="hub.example.test",
        protocol="palimpsest",
        api_base="https://hub.example.test/v1",
        namespace="p-" + "1" * 32,
    )


@pytest.mark.parametrize("option", [["--load"], ["--cache-to", "type=registry,ref=hub.example.test/cache"]])
def test_native_build_rejects_docker_side_effects_before_credential_or_builder_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    option: list[str],
) -> None:
    roots = cli.init_roots()
    registry.save_registry_config(roots, registry.add_profile(registry.default_registry_config(), _native_profile()))
    monkeypatch.setattr(cli, "NativePackageClient", lambda *_args, **_kwargs: pytest.fail("credentials read"))
    monkeypatch.setattr(cli, "build_with_buildkit", lambda *_args, **_kwargs: pytest.fail("builder started"))
    output = tmp_path / "output.oci.tar"
    assert (
        cli.main(["build", str(tmp_path), "--registry", "cloud", "-t", "test:v1", "--output", str(output), *option])
        == 1
    )
    assert not output.exists()
    assert not tuple(roots.builds.glob("bk-*"))


def test_oci_online_build_requires_explicit_native_cache_authority_before_builder_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = cli.init_roots()
    monkeypatch.setenv("PALIMPSEST_TOKEN", "unqualified-token-must-not-authorize-cache")
    monkeypatch.setattr(cli, "HubClient", lambda *_args, **_kwargs: pytest.fail("legacy authority accessed"))
    monkeypatch.setattr(cli, "build_with_buildkit", lambda *_args, **_kwargs: pytest.fail("builder started"))
    output = tmp_path / "output.oci.tar"
    assert cli.main(["build", str(tmp_path), "-t", "test:v1", "--output", str(output)]) == 1
    assert not output.exists()
    assert not tuple(roots.builds.glob("bk-*"))


@pytest.mark.parametrize("operation", ["push", "pull"])
def test_native_all_tags_never_falls_back_to_docker_or_requests_credentials(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    roots = cli.init_roots()
    registry.save_registry_config(roots, registry.add_profile(registry.default_registry_config(), _native_profile()))
    monkeypatch.setattr(cli, "NativePackageClient", lambda *_args, **_kwargs: pytest.fail("credentials read"))
    monkeypatch.setattr(cli, "run_docker_passthrough", lambda *_args, **_kwargs: pytest.fail("Docker invoked"))
    assert cli.main([operation, "test", "--registry", "cloud", "--all-tags"]) == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["push", "test:v1", "--registry", "cloud"], ("packages:inventory", "packages:write")),
        (["pull", "test:v1", "--registry", "cloud"], ("packages:inventory", "packages:read")),
        (
            ["build", "{context}", "--registry", "cloud", "-t", "test:v1", "--push", "--output", "{output}"],
            ("packages:inventory", "packages:write"),
        ),
        (
            ["build", "{context}", "--registry", "cloud", "-t", "test:v1", "--output", "{output}"],
            ("cache:read", "cache:write"),
        ),
    ],
)
def test_native_commands_preflight_exact_capability_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected: tuple[str, ...],
) -> None:
    from palimpsest_local.packages import PackageError

    roots = cli.init_roots()
    profile = _native_profile()
    registry.save_registry_config(roots, registry.add_profile(registry.default_registry_config(), profile))
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    class DeniedClient:
        def __init__(self, selected: RegistryProfile, *, namespace: str) -> None:
            assert (selected.alias, selected.protocol) == (profile.alias, "palimpsest")
            self.namespace = namespace

        def authorize(self, package: str, actions: tuple[str, ...]) -> None:
            calls.append((self.namespace, package, tuple(actions)))
            raise PackageError("native key does not authorize the requested actions")

    monkeypatch.setattr(cli, "NativePackageClient", DeniedClient)
    monkeypatch.setattr(cli, "build_with_buildkit", lambda *_args, **_kwargs: pytest.fail("builder started"))
    monkeypatch.setattr(cli, "run_docker_passthrough", lambda *_args, **_kwargs: pytest.fail("Docker invoked"))
    output = tmp_path / "output.oci.tar"
    command = [value.format(context=tmp_path, output=output) for value in argv]
    assert cli.main(command) == 1
    assert calls == [(profile.namespace, "test", expected)]
    assert not output.exists()
    assert not tuple(roots.state.glob("package-artifacts/*"))


def test_native_reference_roundtrip_rejects_identity_changes_and_shared_files(tmp_path: Path) -> None:
    from dataclasses import replace

    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.package_reference import (
        LocalPackageReference,
        read_package_reference,
        write_package_reference,
    )

    roots = cli.init_roots()
    profile = _native_profile()
    reference = profile.endpoint + "/" + profile.namespace + "/test:v1"
    with pytest.raises(ArtifactValidationError, match="build first or supply --input"):
        read_package_reference(roots, reference)
    record = LocalPackageReference(
        reference=reference,
        authority=profile.endpoint,
        api_base=profile.api_base,
        namespace=profile.namespace,
        package="test",
        archive=str(tmp_path / "image.tar"),
        archive_digest="sha256:" + "b" * 64,
        archive_size_bytes=1024,
        root_digest="sha256:" + "c" * 64,
        package_type="oci-image",
        project_id="F" * 64,
    )
    path = write_package_reference(roots, record)
    assert read_package_reference(roots, reference) == record
    with pytest.raises(ArtifactValidationError):
        replace(record, namespace="other")
    with pytest.raises(ArtifactValidationError):
        replace(record, published_digest="sha256:" + "d" * 64)
    path.chmod(0o644)
    with pytest.raises(ArtifactValidationError):
        read_package_reference(roots, reference)
