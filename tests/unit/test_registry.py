"""Registry profile, Docker reference, and shell-free command contracts."""

from __future__ import annotations

import subprocess
import threading
import tomllib
from pathlib import Path

import pytest

import palimpsest_local.registry as registry
from palimpsest_local.registry import RegistryConfig, RegistryError, RegistryProfile
from palimpsest_local.state import StatePaths, init_roots, permission_bits

DIGEST = "sha256:" + "a" * 64


def _roots(tmp_path: Path) -> StatePaths:
    return init_roots(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )


def _config_with_private() -> RegistryConfig:
    private = RegistryProfile(
        alias="corp",
        endpoint="registry.example.com:5000",
        namespace="engineering/runtime",
        mirrors=("mirror-a.example.com", "mirror-b.example.com:5443"),
        ca=("/etc/palimpsest/corp-ca.pem",),
        tls_skip_verify=True,
        cache_from=("type=registry,ref=registry.example.com:5000/cache/from",),
        cache_to=("type=registry,ref=registry.example.com:5000/cache/to,mode=max",),
    )
    return registry.use_profile(registry.add_profile(registry.default_registry_config(), private), "corp")


def test_registry_config_round_trip_is_canonical_and_owner_only(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    config = _config_with_private()

    registry.save_registry_config(roots, config)

    path = registry.registry_config_path(roots)
    assert path == tmp_path / "config" / "palimpsest" / "registries.toml"
    assert permission_bits(path) == 0o600
    assert permission_bits(path.parent) == 0o700
    assert registry.load_registry_config(roots) == config
    assert registry.render_registry_config(config) == path.read_text(encoding="utf-8")
    assert tomllib.loads(path.read_text(encoding="utf-8"))["default"] == "corp"
    assert registry.registry_config_digest(config) == registry.registry_config_digest(
        registry.load_registry_config(roots)
    )


def test_missing_config_has_builtin_docker_default(tmp_path: Path) -> None:
    config = registry.load_registry_config(_roots(tmp_path))
    assert config.default == "docker"
    assert registry.inspect_profile(config) == RegistryProfile("docker", "docker.io", "library")
    assert not registry.registry_config_path(_roots(tmp_path)).exists()


def test_docker_config_resolution_reuses_docker_store_without_creating_it(tmp_path: Path) -> None:
    configured = tmp_path / "shared-docker-config"
    assert registry.resolve_docker_config_dir({"DOCKER_CONFIG": str(configured), "HOME": str(tmp_path)}) == configured
    assert not configured.exists()
    assert registry.resolve_docker_config_dir({"HOME": str(tmp_path)}) == tmp_path / ".docker"
    assert not (tmp_path / ".docker").exists()


def test_pure_profile_operations_do_not_mutate_source() -> None:
    original = registry.default_registry_config()
    profile = RegistryProfile("corp", "registry.example.com", "team")
    added = registry.add_profile(original, profile)
    selected = registry.use_profile(added, "corp")
    removed = registry.remove_profile(selected, "corp")

    assert tuple(original.registries) == ("docker",)
    assert [item.alias for item in registry.list_profiles(added)] == ["corp", "docker"]
    assert registry.inspect_profile(selected).alias == "corp"
    assert removed.default == "docker"
    with pytest.raises(TypeError):
        original.registries["mutate"] = profile  # type: ignore[index]
    with pytest.raises(RegistryError, match="cannot be removed"):
        registry.remove_profile(original, "docker")


def test_selector_priority_is_explicit_then_environment_then_config_default() -> None:
    config = _config_with_private()
    config = registry.add_profile(config, RegistryProfile("staging", "staging.example.com", "images"))

    assert registry.select_registry_alias(config, environment={}) == "corp"
    assert registry.select_registry_alias(config, environment={"PALIMPSEST_REGISTRY": "staging"}) == "staging"
    assert (
        registry.select_registry_alias(
            config,
            explicit_alias="docker",
            environment={"PALIMPSEST_REGISTRY": "staging"},
        )
        == "docker"
    )


def test_concurrent_transactional_updates_do_not_lose_profiles(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    barrier = threading.Barrier(12)
    failures: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            profile = RegistryProfile(f"r{index}", f"r{index}.example.com", "images")
            registry.update_registry_config(roots, lambda config: registry.add_profile(config, profile))
        except BaseException as exc:  # pragma: no cover - only populated on failure
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert set(registry.load_registry_config(roots).registries) == {"docker", *(f"r{i}" for i in range(12))}
    assert registry.registry_lock_path(roots).parent == roots.locks
    assert permission_bits(registry.registry_lock_path(roots)) == 0o600


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://registry.example.com",
        "registry.example.com/path",
        "user:password@registry.example.com",
        "registry.example.com:notaport",
        "registry.example.com:0",
        "registry.example.com:65536",
        "2001:db8::1",
        "bad_host.example.com",
        "registry.example.com?token=x",
    ],
)
def test_profile_rejects_invalid_or_credential_bearing_endpoint(endpoint: str) -> None:
    with pytest.raises(RegistryError):
        RegistryProfile("corp", endpoint, "team")


def test_profile_validates_transport_cache_and_ca_fields() -> None:
    assert RegistryProfile("ipv6", "[2001:DB8::1]:5000", "images").endpoint == "[2001:db8::1]:5000"
    assert RegistryProfile(
        "mirrorpath",
        "registry.example.com",
        mirrors=("CORE.HARBOR.DOMAIN/proxy.docker.io",),
    ).mirrors == ("core.harbor.domain/proxy.docker.io",)
    with pytest.raises(RegistryError, match="cannot both"):
        RegistryProfile("corp", "registry.example.com", "team", plain_http=True, tls_skip_verify=True)
    with pytest.raises(RegistryError, match="absolute"):
        RegistryProfile("corp", "registry.example.com", "team", ca=("relative.pem",))
    with pytest.raises(RegistryError, match="cache type"):
        RegistryProfile("corp", "registry.example.com", "team", cache_from=("ref=registry.example.com/cache",))
    with pytest.raises(RegistryError, match="credentials or secrets"):
        RegistryProfile(
            "corp",
            "registry.example.com",
            "team",
            cache_to=("type=registry,ref=https://user:pass@registry.example.com/cache",),
        )
    with pytest.raises(RegistryError, match="secret"):
        RegistryProfile(
            "corp",
            "registry.example.com",
            "team",
            cache_from=("type=registry,ref=user:password@registry.example.com/cache",),
        )
    with pytest.raises(RegistryError, match="secret"):
        RegistryProfile(
            "corp",
            "registry.example.com",
            "team",
            cache_to=("type=gha,ghtoken=ghp_do_not_store",),
        )
    assert RegistryProfile(
        "corp",
        "registry.example.com",
        "team",
        cache_from=("registry.example.com/team/cache:latest",),
    ).cache_from == ("registry.example.com/team/cache:latest",)
    digest_cache_ref = "type=registry,ref=repo:tag@sha256:" + "a" * 64
    assert registry.validate_cache_spec(digest_cache_ref) == digest_cache_ref


def test_custom_profile_namespace_defaults_to_empty() -> None:
    profile = RegistryProfile("corp", "registry.example.com")
    assert profile.namespace == ""
    config = registry.add_profile(registry.default_registry_config(), profile)
    assert (
        registry.resolve_image_reference("app", config, registry_alias="corp", environment={}).canonical
        == "registry.example.com/app:latest"
    )


def test_insecure_or_secret_bearing_config_is_rejected(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    path = registry.registry_config_path(roots)
    path.write_text(
        """schema_version = 1
default = "docker"
password = "do-not-store-this"
[registries.docker]
endpoint = "docker.io"
namespace = "library"
""",
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(RegistryError, match="secret"):
        registry.load_registry_config(roots)

    path.write_text(registry.render_registry_config(registry.default_registry_config()), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(RegistryError, match="owner-only"):
        registry.load_registry_config(roots)


def test_docker_hub_short_reference_resolution() -> None:
    config = registry.default_registry_config()
    assert (
        registry.resolve_image_reference("alpine", config, environment={}).canonical
        == "docker.io/library/alpine:latest"
    )
    assert registry.resolve_image_reference("user/app:v1", config, environment={}).canonical == "docker.io/user/app:v1"
    assert (
        registry.resolve_image_reference("docker.io/alpine", config, environment={}).canonical
        == "docker.io/library/alpine:latest"
    )
    pinned = registry.resolve_image_reference(f"alpine@{DIGEST}", config, environment={}, require_digest=True)
    assert pinned.canonical == f"docker.io/library/alpine@{DIGEST}"
    assert pinned.source_id == f"digest:{DIGEST}"


def test_external_registry_in_reference_overrides_all_profile_selection() -> None:
    config = _config_with_private()
    resolved = registry.resolve_image_reference(
        "quay.io/acme/app:stable",
        config,
        registry_alias="not-a-real-profile",
        environment={"PALIMPSEST_REGISTRY": "also-invalid"},
    )
    assert resolved.canonical == "quay.io/acme/app:stable"
    assert resolved.registry_alias is None


def test_selected_profile_applies_endpoint_and_single_name_namespace() -> None:
    config = _config_with_private()
    assert (
        registry.resolve_image_reference("worker:v2", config, environment={}).canonical
        == "registry.example.com:5000/engineering/runtime/worker:v2"
    )
    assert (
        registry.resolve_image_reference("other/worker:v2", config, environment={}).canonical
        == "registry.example.com:5000/other/worker:v2"
    )
    assert (
        registry.resolve_image_reference("worker:v2", config, registry_alias="docker", environment={}).canonical
        == "docker.io/library/worker:v2"
    )


@pytest.mark.parametrize(
    "reference",
    [
        "app@sha256:abc",
        "app@sha512:" + "a" * 64,
        "App:latest",
        "example.com/UPPER/app:latest",
        "app:bad tag",
        "app@@" + DIGEST,
    ],
)
def test_invalid_image_references_are_rejected(reference: str) -> None:
    with pytest.raises(RegistryError):
        registry.resolve_image_reference(reference, registry.default_registry_config(), environment={})


def test_digest_is_required_when_requested() -> None:
    with pytest.raises(RegistryError, match="digest-pinned"):
        registry.resolve_image_reference(
            "alpine:latest",
            registry.default_registry_config(),
            environment={},
            require_digest=True,
        )


def test_docker_argv_helpers_keep_config_and_subcommands_exact(tmp_path: Path) -> None:
    config_dir = tmp_path / "docker-config"
    prefix = ["docker", "--config", str(config_dir.resolve())]

    assert registry.docker_login_argv(
        config_dir,
        "registry.example.com",
        username="alice",
        password_stdin=True,
    ) == [
        *prefix,
        "login",
        "--username",
        "alice",
        "--password-stdin",
        "registry.example.com",
    ]
    assert registry.docker_logout_argv(config_dir, "registry.example.com") == [
        *prefix,
        "logout",
        "registry.example.com",
    ]
    assert registry.docker_login_argv(config_dir, "docker.io") == [*prefix, "login", "docker.io"]
    assert registry.docker_pull_argv(config_dir, "image:v1", platform="linux/amd64", quiet=True) == [
        *prefix,
        "pull",
        "--platform",
        "linux/amd64",
        "--quiet",
        "image:v1",
    ]
    assert registry.docker_push_argv(config_dir, "image:v1", platform="linux/amd64", all_tags=True) == [
        *prefix,
        "push",
        "--platform",
        "linux/amd64",
        "--all-tags",
        "image:v1",
    ]
    assert registry.docker_tag_argv(config_dir, "source:v1", "target:v1") == [
        *prefix,
        "tag",
        "source:v1",
        "target:v1",
    ]
    assert registry.docker_image_inspect_argv(config_dir, ["image:v1"], platform="linux/arm64") == [
        *prefix,
        "image",
        "inspect",
        "--platform",
        "linux/arm64",
        "image:v1",
    ]
    assert registry.docker_image_rm_argv(config_dir, ["image:v1"], force=True) == [
        *prefix,
        "image",
        "rm",
        "--force",
        "image:v1",
    ]
    assert registry.docker_history_argv(config_dir, "image:v1", no_trunc=True) == [
        *prefix,
        "image",
        "history",
        "--no-trunc",
        "image:v1",
    ]
    assert registry.docker_save_argv(config_dir, ["image:v1"], output=tmp_path / "image.tar") == [
        *prefix,
        "image",
        "save",
        "--output",
        str((tmp_path / "image.tar").resolve()),
        "image:v1",
    ]
    assert registry.docker_load_argv(
        config_dir,
        input_path=tmp_path / "image.tar",
        platforms=("linux/amd64",),
        quiet=True,
    ) == [
        *prefix,
        "image",
        "load",
        "--input",
        str((tmp_path / "image.tar").resolve()),
        "--platform",
        "linux/amd64",
        "--quiet",
    ]


def test_generic_docker_runner_never_uses_shell_and_login_password_stays_off_argv(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    password = "correct horse battery staple"

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "Login Succeeded\n", "")

    result = registry.run_docker_login(
        tmp_path / "docker",
        "registry.example.com",
        password,
        username="alice",
        runner=runner,
    )

    assert result.returncode == 0
    argv, kwargs = calls[0]
    assert password not in argv
    assert kwargs["input"] == f"{password}\n"
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert "--password-stdin" in argv
    with pytest.raises(RegistryError, match="only through"):
        registry.docker_command_argv(tmp_path / "docker", "login", "--password", password, "example.com")
    with pytest.raises(RegistryError, match="only through"):
        registry.docker_command_argv(
            tmp_path / "docker",
            "--context",
            "remote",
            "login",
            "-p",
            password,
            "example.com",
        )
    with pytest.raises(RegistryError, match="DOCKER_CONFIG"):
        registry.docker_command_argv(tmp_path / "docker", "--config", "/tmp/other", "version")
    assert registry.docker_command_argv(
        tmp_path / "docker",
        "run",
        "app",
        "program",
        "--config=/etc/app",
    )[3:] == ["run", "app", "program", "--config=/etc/app"]
    assert registry.docker_command_argv(tmp_path / "docker", "run", "-p", "8080:80", "nginx")[3:] == [
        "run",
        "-p",
        "8080:80",
        "nginx",
    ]


def test_passthrough_runner_inherits_stdio_and_preserves_docker_exit_code(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    argv = registry.docker_login_argv(tmp_path / "docker", "docker.io")

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[None]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 17, None, None)

    result = registry.run_docker_passthrough(argv, runner=runner)

    assert result.returncode == 17
    assert calls == [(argv, {"shell": False, "check": False})]
    assert all(name not in calls[0][1] for name in ("stdin", "stdout", "stderr", "input", "capture_output"))


def test_all_requested_docker_command_families_have_shell_free_argv(tmp_path: Path) -> None:
    images = registry.docker_images_argv(
        tmp_path,
        "repo",
        all_images=True,
        digests=True,
        filters=("dangling=false",),
        output_format="{{.Repository}}",
    )
    assert images[3:] == [
        "images",
        "--all",
        "--digests",
        "--filter",
        "dangling=false",
        "--format",
        "{{.Repository}}",
        "repo",
    ]
    assert registry.docker_command_argv(tmp_path, "version")[3:] == ["version"]


def test_buildkitd_renderer_maps_mirrors_http_insecure_and_ca() -> None:
    config = registry.default_registry_config()
    config = registry.add_profile(
        config,
        RegistryProfile(
            "plain",
            "plain.example.com:5000",
            "images",
            mirrors=("mirror.example.com:5001",),
            plain_http=True,
        ),
    )
    config = registry.add_profile(
        config,
        RegistryProfile(
            "private",
            "private.example.com",
            "images",
            ca=("/etc/ssl/private-ca.pem",),
            tls_skip_verify=True,
        ),
    )

    rendered = registry.render_buildkitd_toml(config)
    parsed = tomllib.loads(rendered)
    assert parsed["registry"]["docker.io"] == {}
    assert parsed["registry"]["plain.example.com:5000"] == {
        "mirrors": ["mirror.example.com:5001"],
        "http": True,
    }
    assert parsed["registry"]["private.example.com"] == {
        "ca": ["/etc/ssl/private-ca.pem"],
        "insecure": True,
    }


def test_buildkitd_renderer_rejects_conflicting_transport_for_same_endpoint() -> None:
    config = registry.default_registry_config()
    config = registry.add_profile(config, RegistryProfile("one", "shared.example.com", "one"))
    config = registry.add_profile(
        config,
        RegistryProfile("two", "shared.example.com", "two", tls_skip_verify=True),
    )
    with pytest.raises(RegistryError, match="conflicting"):
        registry.render_buildkitd_toml(config)
