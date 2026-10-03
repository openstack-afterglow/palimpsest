"""Native credential permission boundaries; helpers never fall back to host auth."""

from __future__ import annotations

import json
import subprocess
import traceback
from pathlib import Path

import pytest

from palimpsest_local import package_credentials as credentials
from palimpsest_local.registry import RegistryError, RegistryProfile

PUBLIC_ID = "11111111111141118111111111111111"
KEY = "ppk_v1_" + PUBLIC_ID + "." + "A" * 43


def _profile(api_base: str = "https://cloud.example.com/api/v1/hub") -> RegistryProfile:
    return RegistryProfile("cloud", "cloud.example.com", "team", protocol="palimpsest", api_base=api_base)


def _environment(tmp_path: Path, config: dict[str, object], *, installed: bool = True) -> dict[str, str]:
    docker = tmp_path / "docker"
    docker.mkdir()
    (docker / "config.json").write_text(json.dumps(config), encoding="utf-8")
    binary = tmp_path / "bin"
    binary.mkdir()
    if installed:
        helper = binary / "docker-credential-safe"
        helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        helper.chmod(0o700)
    return {"DOCKER_CONFIG": str(docker), "HOME": str(tmp_path), "PATH": str(binary)}


def test_exact_namespace_helper_overrides_store_and_host_auth_is_not_read(tmp_path: Path) -> None:
    server_url = "https://cloud.example.com/api/v1/hub/projects/team"
    environment = _environment(
        tmp_path,
        {
            "credHelpers": {server_url: "safe", "cloud.example.com": "unavailable"},
            "credsStore": "unavailable",
            "auths": {"cloud.example.com": {"auth": "plaintext-not-a-package-key"}},
        },
    )
    helper = credentials.require_credential_helper(_profile(), "team", environment=environment)
    assert helper.server_url == server_url
    assert helper.executable == str(tmp_path / "bin" / "docker-credential-safe")
    with pytest.raises(RegistryError):
        credentials.require_credential_helper(_profile(), "other", environment=environment)
    with pytest.raises(RegistryError):
        credentials.require_credential_helper(
            _profile("https://cloud.example.com/another/v1"), "team", environment=environment
        )


def test_only_configured_global_store_is_a_fallback(tmp_path: Path) -> None:
    environment = _environment(
        tmp_path,
        {
            "credHelpers": {"cloud.example.com": "missing"},
            "credsStore": "safe",
            "auths": {"cloud.example.com": {"auth": "do-not-use"}},
        },
    )
    assert credentials.require_credential_helper(_profile(), "other", environment=environment).server_url.endswith(
        "/projects/other"
    )


@pytest.mark.parametrize(
    "config",
    [
        {"auths": {"cloud.example.com": {"auth": "aWdub3Jl"}}},
        {"credHelpers": {"cloud.example.com": "safe"}},
        {"credHelpers": {"https://cloud.example.com/api/v1/hub/projects/team": ""}, "credsStore": "safe"},
        {"credsStore": "safe;echo secret"},
        {"credsStore": "../safe"},
    ],
)
def test_unconfigured_or_invalid_helper_cannot_fall_back_to_plaintext(
    tmp_path: Path, config: dict[str, object]
) -> None:
    environment = _environment(tmp_path, config)
    environment["PALIMPSEST_TOKEN"] = KEY
    environment["PALIMPSEST_PACKAGE_KEY"] = KEY
    with pytest.raises(RegistryError):
        credentials.get_package_key(_profile(), "team", environment=environment)


def test_helper_availability_preflight_requires_executable_permission(tmp_path: Path) -> None:
    environment = _environment(tmp_path, {"credsStore": "safe"}, installed=False)
    with pytest.raises(RegistryError, match="not installed or executable"):
        credentials.require_credential_helper(_profile(), "team", environment=environment)
    helper = tmp_path / "bin" / "docker-credential-safe"
    helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    helper.chmod(0o600)
    with pytest.raises(RegistryError, match="not installed or executable"):
        credentials.require_credential_helper(_profile(), "team", environment=environment)


def test_helper_store_get_and_erase_keep_secret_on_stdin_and_namespace_isolated(tmp_path: Path) -> None:
    environment = _environment(tmp_path, {"credsStore": "safe"})
    environment["PALIMPSEST_PACKAGE_KEY"] = KEY
    environment["PALIMPSEST_TOKEN"] = "legacy-secret"
    entries: dict[str, dict[str, str]] = {}
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        assert KEY not in " ".join(argv)
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        assert "PALIMPSEST_PACKAGE_KEY" not in kwargs["env"]
        assert "PALIMPSEST_TOKEN" not in kwargs["env"]
        payload = str(kwargs["input"])
        if argv[1] == "store":
            record = json.loads(payload)
            entries[record["ServerURL"]] = record
            return subprocess.CompletedProcess(argv, 0, KEY, KEY)
        if argv[1] == "get":
            record = entries.get(payload.strip())
            return subprocess.CompletedProcess(argv, 0 if record else 1, json.dumps(record), KEY)
        del entries[payload.strip()]
        return subprocess.CompletedProcess(argv, 0, KEY, KEY)

    credentials.store_package_key(_profile(), "team", KEY, PUBLIC_ID, environment=environment, runner=runner)
    assert credentials.get_package_key(_profile(), "team", environment=environment, runner=runner) == KEY
    with pytest.raises(RegistryError):
        credentials.get_package_key(_profile(), "other", environment=environment, runner=runner)
    credentials.erase_package_key(_profile(), "team", environment=environment, runner=runner)
    with pytest.raises(RegistryError):
        credentials.get_package_key(_profile(), "team", environment=environment, runner=runner)
    assert (
        calls[0][1]["input"]
        == json.dumps(
            {
                "ServerURL": "https://cloud.example.com/api/v1/hub/projects/team",
                "Username": PUBLIC_ID,
                "Secret": KEY,
            }
        )
        + "\n"
    )


@pytest.mark.parametrize("username", ["someone", "22222222222242228222222222222222", None])
def test_helper_response_cannot_substitute_another_public_identity(tmp_path: Path, username: object) -> None:
    environment = _environment(tmp_path, {"credsStore": "safe"})

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps({"Username": username, "Secret": KEY}), "")

    with pytest.raises(RegistryError, match="public UUID"):
        credentials.get_package_key(_profile(), "team", environment=environment, runner=runner)
    with pytest.raises(RegistryError, match="public UUID"):
        credentials.store_package_key(_profile(), "team", KEY, username, environment=environment, runner=runner)


@pytest.mark.parametrize(
    "credential",
    ["raw-keystone-token", KEY + "\n", KEY[:-1] + "B", KEY.replace(PUBLIC_ID, PUBLIC_ID.upper() + "f"), None],
)
def test_wire_key_requires_canonical_public_id_and_exact_32_byte_secret(credential: object) -> None:
    with pytest.raises(RegistryError, match="key format"):
        credentials.validate_package_key(credential)


@pytest.mark.parametrize("failure", ["exit", "exception", "invalid-json"])
def test_helper_secret_output_and_errors_are_not_exposed(tmp_path: Path, failure: str) -> None:
    environment = _environment(tmp_path, {"credsStore": "safe"})

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure == "exception":
            raise subprocess.TimeoutExpired(argv, 30, output=KEY, stderr=KEY)
        return subprocess.CompletedProcess(argv, 1 if failure == "exit" else 0, KEY, KEY)

    with pytest.raises(RegistryError) as caught:
        credentials.get_package_key(_profile(), "team", environment=environment, runner=runner)
    assert KEY not in str(caught.value)
    assert KEY not in "".join(traceback.format_exception(caught.type, caught.value, caught.tb))


def test_namespace_and_protocol_are_required_before_helper_access(tmp_path: Path) -> None:
    environment = _environment(tmp_path, {"credsStore": "safe"})
    for namespace in ("", "team/other", "a" * 64):
        with pytest.raises(RegistryError):
            credentials.require_credential_helper(_profile(), namespace, environment=environment)
    with pytest.raises(RegistryError, match="palimpsest profile"):
        credentials.require_credential_helper(
            RegistryProfile("oci", "cloud.example.com", "team"), "team", environment=environment
        )
