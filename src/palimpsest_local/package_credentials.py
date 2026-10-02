"""Native package credentials through configured Docker helpers, never auth JSON.

The helper ServerURL is exactly ``api_base.rstrip('/') + '/projects/' + namespace``.
Only the package client may accept PALIMPSEST_PACKAGE_KEY and it must authenticate
that ephemeral credential through /auth/me before any package or cache effects.
These functions do not accept PALIMPSEST_TOKEN or persist environment credentials.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .registry import (
    RegistryError,
    RegistryProfile,
    credential_free_subprocess_environment,
    normalize_native_namespace,
    resolve_docker_config_dir,
)

_KEY_RE = re.compile(r"ppk_v1_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})", re.ASCII)
_HELPER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", re.ASCII)


def validate_package_key(credential: object) -> str:
    """Validate the complete canonical wire key and return its public UUID hex."""
    match = _KEY_RE.fullmatch(credential) if isinstance(credential, str) else None
    if match is None:
        raise RegistryError("invalid native package key format")
    try:
        secret = base64.urlsafe_b64decode(match[2] + "=")
    except (ValueError, binascii.Error):
        raise RegistryError("invalid native package key format") from None
    if len(secret) != 32 or base64.urlsafe_b64encode(secret).decode("ascii").rstrip("=") != match[2]:
        raise RegistryError("invalid native package key format")
    return match[1]


def validate_package_username(username: object, public_id: str) -> None:
    """Require the canonical public UUID (hex or hyphenated), not an arbitrary name."""
    if not isinstance(username, str):
        raise RegistryError("native credential username must match the package key public UUID")
    try:
        identity = UUID(username)
    except ValueError:
        raise RegistryError("native credential username must match the package key public UUID") from None
    if username not in {identity.hex, str(identity)} or identity.hex != public_id:
        raise RegistryError("native credential username must match the package key public UUID")


def helper_server_url(profile: RegistryProfile, namespace: str | None = None) -> str:
    """Return the exact namespace-qualified Docker credential-helper lookup key."""
    if profile.protocol != "palimpsest":
        raise RegistryError("native package credentials require a palimpsest profile")
    selected = normalize_native_namespace(profile.namespace if namespace is None else namespace)
    return profile.api_base.rstrip("/") + "/projects/" + selected


@dataclass(frozen=True)
class CredentialHelper:
    """Preflight result containing no credential material."""

    executable: str
    server_url: str


def require_credential_helper(
    profile: RegistryProfile,
    namespace: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> CredentialHelper:
    """Resolve the exact credHelpers entry, then only the configured credsStore.

    No host-only helper entry, auths record, or plaintext fallback is consulted.
    Missing/invalid configuration or an unavailable executable fails before login
    can authenticate or persist a key. This function does not execute the helper.
    """
    server_url = helper_server_url(profile, namespace)
    env = os.environ if environment is None else environment
    config_path = resolve_docker_config_dir(env) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise RegistryError("native login requires a readable Docker credential-helper configuration") from None
    if not isinstance(config, dict):
        raise RegistryError("invalid Docker credential-helper configuration")
    helpers = config.get("credHelpers", {})
    if not isinstance(helpers, dict):
        raise RegistryError("invalid Docker credHelpers configuration")
    helper = helpers[server_url] if server_url in helpers else config.get("credsStore")
    if not isinstance(helper, str) or _HELPER_RE.fullmatch(helper) is None:
        raise RegistryError(
            "native credentials require a configured Docker credential helper for the exact namespace key"
        )
    executable = shutil.which("docker-credential-" + helper, path=env.get("PATH", os.defpath))
    if executable is None:
        raise RegistryError("configured Docker credential helper is not installed or executable")
    return CredentialHelper(executable=executable, server_url=server_url)


def _run_helper(
    helper: CredentialHelper,
    operation: str,
    payload: str,
    *,
    environment: Mapping[str, str] | None,
    runner: Callable[..., Any],
) -> str:
    # Helpers receive their standard input only; no secret is ever an argv value.
    env = credential_free_subprocess_environment(environment)
    try:
        result = runner(
            [helper.executable, operation],
            input=payload,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=30,
            env=env,
        )
    except Exception:
        # A helper exception/timeout may embed stdin, stdout, stderr, or secrets.
        raise RegistryError("Docker credential helper failed") from None
    if result.returncode != 0:
        raise RegistryError("Docker credential helper failed")
    return result.stdout if operation == "get" else ""


def get_package_key(
    profile: RegistryProfile,
    namespace: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Read a key from the configured helper; validate its public username."""
    helper = require_credential_helper(profile, namespace, environment=environment)
    output = _run_helper(helper, "get", helper.server_url + "\n", environment=environment, runner=runner)
    try:
        record = json.loads(output)
    except (ValueError, TypeError):
        raise RegistryError("Docker credential helper returned invalid native credentials") from None
    if not isinstance(record, dict):
        raise RegistryError("Docker credential helper returned invalid native credentials")
    credential = record.get("Secret")
    public_id = validate_package_key(credential)
    validate_package_username(record.get("Username"), public_id)
    return credential


def store_package_key(
    profile: RegistryProfile,
    namespace: str,
    credential: str,
    username: str,
    *,
    environment: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Persist an already /auth/me-validated key; CLI owns authentication order."""
    public_id = validate_package_key(credential)
    validate_package_username(username, public_id)
    helper = require_credential_helper(profile, namespace, environment=environment)
    payload = json.dumps({"ServerURL": helper.server_url, "Username": public_id, "Secret": credential}) + "\n"
    _run_helper(helper, "store", payload, environment=environment, runner=runner)


def erase_package_key(
    profile: RegistryProfile,
    namespace: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Erase only the configured API-base/namespace entry, not Docker host auth."""
    helper = require_credential_helper(profile, namespace, environment=environment)
    _run_helper(helper, "erase", helper.server_url + "\n", environment=environment, runner=runner)
