"""Registry profile, Docker reference, and shell-free command contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def test_qualified_registry_keeps_unconfigured_authority_oci_and_rejects_mismatched_alias() -> None:
    config = _config_with_private()
    # Only a configured native profile can select native transport; others stay Docker/OCI.
    unconfigured = registry.resolve_image_reference("quay.io/acme/app:stable", config, environment={})
    assert (unconfigured.canonical, unconfigured.registry_alias) == ("quay.io/acme/app:stable", None)
    with pytest.raises(RegistryError, match="does not match"):
        registry.resolve_image_reference("quay.io/acme/app:stable", config, registry_alias="corp", environment={})
    config = registry.add_profile(config, RegistryProfile("quay", "quay.io"))
    resolved = registry.resolve_image_reference(
        "quay.io/acme/app:stable", config, environment={"PALIMPSEST_REGISTRY": "also-invalid"}
    )
    assert resolved.canonical == "quay.io/acme/app:stable"
    assert resolved.registry_alias == "quay"
    with pytest.raises(RegistryError, match="does not match"):
        registry.resolve_image_reference("quay.io/acme/app:stable", config, registry_alias="corp", environment={})


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


def test_native_profile_round_trip_preserves_api_base_and_legacy_profiles_are_oci(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    path = registry.registry_config_path(roots)
    path.write_text(
        'schema_version = 1\ndefault = "docker"\n[registries.docker]\nendpoint = "docker.io"\nnamespace = "library"\n',
        encoding="utf-8",
    )
    path.chmod(0o600)
    config = registry.load_registry_config(roots)
    assert config.registries["docker"].protocol == "oci"
    native = RegistryProfile(
        "cloud",
        "cloud.example.com",
        "team",
        protocol="palimpsest",
        api_base="https://cloud.example.com/api/v1/palimpsest/hub/",
    )
    config = registry.add_profile(config, native)
    registry.save_registry_config(roots, config)
    loaded = registry.load_registry_config(roots)
    assert loaded == config
    assert loaded.registries["cloud"].api_base == "https://cloud.example.com/api/v1/palimpsest/hub"
    assert permission_bits(path) == 0o600
    assert "cloud.example.com" not in tomllib.loads(registry.render_buildkitd_toml(loaded))["registry"]


@pytest.mark.parametrize(
    "api_base",
    [
        "http://cloud.example.com/v1",
        "https://other.example.com/v1",
        "https://cloud.example.com:443/v1",
        "https://user:password@cloud.example.com/v1",
        "https://cloud.example.com/v1?token=secret",
        "https://cloud.example.com/v1#fragment",
        "https://cloud.example.com/a/../v1",
        "https://cloud.example.com/a%2fv1",
        "https://cloud.example.com/a//v1",
    ],
)
def test_native_api_base_cannot_change_authority_or_transport(api_base: str) -> None:
    with pytest.raises(RegistryError):
        RegistryProfile("cloud", "cloud.example.com", protocol="palimpsest", api_base=api_base)


@pytest.mark.parametrize("namespace", ["team/other", "A", ".", "..", "a" * 64, "é", "team%2fother"])
def test_native_namespace_rejects_noncanonical_project_components(namespace: str) -> None:
    with pytest.raises(RegistryError):
        RegistryProfile(
            "cloud", "cloud.example.com", namespace, protocol="palimpsest", api_base="https://cloud.example.com/v1"
        )


@pytest.mark.parametrize(
    "settings",
    [
        {"mirrors": ("mirror.example.com",)},
        {"plain_http": True},
        {"tls_skip_verify": True},
        {"cache_from": ("type=registry,ref=cloud.example.com/cache",)},
        {"cache_to": ("type=registry,ref=cloud.example.com/cache",)},
    ],
)
def test_native_profiles_cannot_enable_docker_transport(settings: dict[str, object]) -> None:
    with pytest.raises(RegistryError, match="native profiles"):
        RegistryProfile(
            "cloud", "cloud.example.com", protocol="palimpsest", api_base="https://cloud.example.com/v1", **settings
        )


def test_native_reference_never_misroutes_to_oci_or_another_authority() -> None:
    native = RegistryProfile(
        "cloud", "cloud.example.com", "team", protocol="palimpsest", api_base="https://cloud.example.com/v1"
    )
    config = registry.add_profile(registry.default_registry_config(), native)
    resolved = registry.resolve_image_reference("app:v1", config, registry_alias="cloud", environment={})
    assert resolved.canonical == "cloud.example.com/team/app:v1"
    assert resolved.registry_alias == "cloud"
    with pytest.raises(RegistryError, match="does not match"):
        registry.resolve_image_reference("docker.io/team/app:v1", config, registry_alias="cloud", environment={})
    config = registry.add_profile(config, RegistryProfile("legacy", "cloud.example.com"))
    with pytest.raises(RegistryError, match="ambiguous"):
        registry.resolve_image_reference(
            "cloud.example.com/team/app:v1", config, registry_alias="cloud", environment={}
        )


def test_native_default_namespace_is_optional_but_short_names_require_it() -> None:
    native = RegistryProfile(
        "cloud", "cloud.example.com", protocol="palimpsest", api_base="https://cloud.example.com/v1"
    )
    config = registry.add_profile(registry.default_registry_config(), native)
    with pytest.raises(RegistryError, match="namespace/package"):
        registry.resolve_image_reference("app:v1", config, registry_alias="cloud", environment={})
    assert (
        registry.resolve_image_reference("cloud.example.com/team/app:v1", config, environment={}).registry_alias
        == "cloud"
    )


@pytest.mark.parametrize(
    "operation",
    [
        "docker-capture",
        "docker-passthrough",
        "buildx-preflight",
        "packer-command",
        "credential-helper",
    ],
)
def test_real_tool_children_cannot_read_palimpsest_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from palimpsest_local import buildkit, package_credentials

    public_id = "11111111111141118111111111111111"
    package_key = "ppk_v1_" + public_id + "." + "A" * 43
    configuration = {
        "DOCKER_CONFIG": str(tmp_path / "docker-config"),
        "DOCKER_CONTEXT": "selected-context",
        "BUILDX_BUILDER": "selected-builder",
    }
    binary = tmp_path / "bin"
    binary.mkdir()
    report = tmp_path / "child.json"
    probe_source = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "names = ('PALIMPSEST_PACKAGE_KEY', 'PALIMPSEST_TOKEN', 'DOCKER_CONFIG', 'DOCKER_CONTEXT', 'BUILDX_BUILDER')\n"
        "observed = {name: os.environ[name] for name in names if name in os.environ}\n"
        "Path(os.environ['PALIMPSEST_TEST_CHILD_REPORT']).write_text(json.dumps({'environment': observed, 'argv': sys.argv[1:]}))\n"
        "if sys.argv[1:3] == ['buildx', 'inspect']:\n"
        "    print('Name: selected-builder\\nDriver: docker-container')\n"
        "if sys.argv[1:2] == ['store']:\n"
        "    supplied = json.loads(sys.stdin.read())\n"
        "    assert supplied['Secret'].startswith('ppk_v1_')\n"
        "sys.exit(17 if os.environ['PALIMPSEST_TEST_CHILD_OPERATION'] == 'docker-passthrough' else 0)\n"
    )
    for executable in ("docker", "docker-credential-probe", "packer-probe"):
        path = binary / executable
        path.write_text(probe_source)
        path.chmod(0o700)
    for name, value in configuration.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ.get("PATH", os.defpath))
    monkeypatch.setenv("PALIMPSEST_PACKAGE_KEY", package_key)
    monkeypatch.setenv("PALIMPSEST_TOKEN", "legacy-credential-never-for-tool-children")
    monkeypatch.setenv("PALIMPSEST_TEST_CHILD_REPORT", str(report))
    monkeypatch.setenv("PALIMPSEST_TEST_CHILD_OPERATION", operation)

    if operation == "docker-capture":
        registry.run_docker_command(registry.docker_command_argv(tmp_path / "docker-config", "version"))
    elif operation == "docker-passthrough":
        result = registry.run_docker_passthrough(registry.docker_command_argv(tmp_path / "docker-config", "version"))
        assert result.returncode == 17
    elif operation == "buildx-preflight":
        assert buildkit.preflight_buildx_oci_exporter() == "docker-container"
    elif operation == "packer-command":
        buildkit._run_checked(subprocess.run, [str(binary / "packer-probe")], operation="packer environment probe")
    else:
        docker = tmp_path / "docker-config"
        docker.mkdir()
        (docker / "config.json").write_text(json.dumps({"credsStore": "probe"}))
        profile = RegistryProfile(
            "cloud", "cloud.example.test", "team", protocol="palimpsest", api_base="https://cloud.example.test/v1"
        )
        package_credentials.store_package_key(profile, "team", package_key, public_id)

    child = json.loads(report.read_text())
    assert child["environment"] == configuration
    assert package_key not in json.dumps(child)
    assert "legacy-credential-never-for-tool-children" not in json.dumps(child)


def test_short_native_reference_cannot_bypass_mixed_protocol_authority():
    native = RegistryProfile(
        "cloud", "cloud.example.com", "team", protocol="palimpsest", api_base="https://cloud.example.com/v1"
    )
    config = registry.add_profile(registry.default_registry_config(), native)
    config = registry.add_profile(config, RegistryProfile("oci", "cloud.example.com"))
    for alias in ("cloud", "oci"):
        with pytest.raises(RegistryError):
            registry.resolve_image_reference("app:v1", config, registry_alias=alias, environment={})


def test_completed_native_reference_enforces_combined_repository_length():
    native = RegistryProfile(
        "cloud", "cloud.example.com", "a" * 63, protocol="palimpsest", api_base="https://cloud.example.com/v1"
    )
    config = registry.add_profile(registry.default_registry_config(), native)
    accepted = registry.resolve_image_reference("b" * 191, config, registry_alias="cloud", environment={})
    assert len(accepted.repository) == 255
    with pytest.raises(RegistryError):
        registry.resolve_image_reference("b" * 192, config, registry_alias="cloud", environment={})
