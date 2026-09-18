"""Owner-only registry profiles and shell-free Docker CLI integration.

Registry credentials intentionally do not live in ``registries.toml``. Docker
continues to own authentication through its selected ``--config`` directory,
which allows an installed Docker credential helper to be used unchanged.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .digest import normalize_digest
from .errors import PalimpsestError
from .state import StatePaths, file_lock, fsync_directory

SCHEMA_VERSION = 1
BUILTIN_ALIAS = "docker"
BUILTIN_ENDPOINT = "docker.io"
BUILTIN_NAMESPACE = "library"

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_REPOSITORY_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_PINNED_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_CACHE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(access[_-]?key(?:[_-]?id)?|api[_-]?key|auth|credential|password|passwd|"
    r"private[_-]?key|secret|session[_-]?token|token|ghtoken|github[_-]?token)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:[?&,](?:access[_-]?key(?:[_-]?id)?|api[_-]?key|auth|credential|password|secret|session[_-]?token|"
    r"token|sig|signature|x-amz-(?:credential|signature|security-token)|x-goog-(?:credential|signature))="
    r"|://[^/@:\s]+:[^/@\s]+@|(?:^|[=,])[^/@:\s,]+:[^/@\s,]+@"
    r"(?:[a-z0-9.-]+|\[[0-9a-f:]+\])(?::[0-9]+)?/|-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_PROFILE_FIELDS = {
    "endpoint",
    "namespace",
    "mirrors",
    "ca",
    "plain_http",
    "tls_skip_verify",
    "cache_from",
    "cache_to",
}
_TOP_LEVEL_FIELDS = {"schema_version", "default", "registries"}
_DOCKER_GLOBAL_OPTIONS_WITH_VALUE = {
    "-c",
    "--context",
    "--config",
    "-H",
    "--host",
    "-l",
    "--log-level",
    "--tlscacert",
    "--tlscert",
    "--tlskey",
}


class RegistryError(PalimpsestError):
    """A registry configuration, reference, or Docker operation is invalid."""


def _require_text(value: object, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RegistryError(f"{field} must be a nonempty string without surrounding whitespace")
    if len(value) > maximum or "\x00" in value or "\r" in value or "\n" in value:
        raise RegistryError(f"invalid {field}")
    return value


def _normalize_alias(value: object) -> str:
    alias = _require_text(value, "registry alias", maximum=63).lower()
    if _ALIAS_RE.fullmatch(alias) is None:
        raise RegistryError("registry aliases must match ^[a-z][a-z0-9_-]{0,62}$")
    return alias


def _split_endpoint(value: str) -> tuple[str, int | None, bool]:
    """Return ``(host, port, ipv6)`` for one scheme-free registry endpoint."""
    if any(marker in value for marker in ("://", "/", "\\", "@", "?", "#")) or any(ch.isspace() for ch in value):
        raise RegistryError("registry endpoint must be host[:port] without scheme, path, or credentials")
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            raise RegistryError("invalid bracketed IPv6 registry endpoint")
        host = value[1:closing]
        suffix = value[closing + 1 :]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].isdigit():
                raise RegistryError("registry endpoint port must be numeric")
            port = int(suffix[1:])
        else:
            port = None
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise RegistryError("invalid IPv6 registry endpoint") from exc
        return host.lower(), port, True
    if value.count(":") > 1:
        raise RegistryError("IPv6 registry endpoints must use brackets")
    host, separator, port_text = value.partition(":")
    port = None
    if separator:
        if not port_text.isdigit():
            raise RegistryError("registry endpoint port must be numeric")
        port = int(port_text)
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        labels = host.lower().split(".")
        if not labels or any(_DNS_LABEL_RE.fullmatch(label) is None for label in labels):
            raise RegistryError("invalid registry endpoint host") from None
    return host.lower(), port, False


def normalize_endpoint(value: object) -> str:
    endpoint = _require_text(value, "registry endpoint", maximum=255)
    host, port, ipv6 = _split_endpoint(endpoint)
    if port is not None and not 1 <= port <= 65535:
        raise RegistryError("registry endpoint port must be between 1 and 65535")
    rendered_host = f"[{host}]" if ipv6 else host
    return f"{rendered_host}:{port}" if port is not None else rendered_host


def _normalize_mirror(value: object) -> str:
    mirror = _require_text(value, "registry mirror", maximum=512)
    endpoint_text, separator, path = mirror.partition("/")
    endpoint = normalize_endpoint(endpoint_text)
    if not separator:
        return endpoint
    return f"{endpoint}/{_normalize_repository_path(path, 'registry mirror path')}"


def _normalize_repository_path(value: object, field: str) -> str:
    path = _require_text(value, field, maximum=255)
    if path.startswith("/") or path.endswith("/"):
        raise RegistryError(f"{field} must not start or end with a slash")
    components = path.split("/")
    if any(_REPOSITORY_COMPONENT_RE.fullmatch(component) is None for component in components):
        raise RegistryError(f"{field} must use lower-case Docker repository components")
    return path


def _normalize_string_tuple(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise RegistryError(f"{field} must be an array of strings")
    items = tuple(_require_text(item, field) for item in value)
    if len(items) > 64:
        raise RegistryError(f"{field} supports at most 64 entries")
    if len(set(items)) != len(items):
        raise RegistryError(f"{field} must not contain duplicates")
    return items


def validate_cache_spec(value: str, field: str = "external cache") -> str:
    """Validate Buildx cache syntax while rejecting inline credentials."""
    value = _require_text(value, field)
    if _SECRET_VALUE_RE.search(value):
        raise RegistryError(f"{field} must not contain inline credentials or secrets")
    if "=" not in value and "," not in value:
        return value
    fields = value.split(",")
    pairs: dict[str, str] = {}
    for item in fields:
        key, separator, content = item.partition("=")
        if not separator or _CACHE_KEY_RE.fullmatch(key) is None or not content:
            raise RegistryError(f"{field} entries must use Docker cache syntax such as type=registry,ref=host/repo")
        if key in pairs:
            raise RegistryError(f"{field} entry contains duplicate key {key!r}")
        if _SECRET_KEY_RE.search(key):
            raise RegistryError(f"{field} must not contain secret-shaped options")
        pairs[key] = content
    if "type" not in pairs:
        raise RegistryError(f"{field} entries must declare a cache type")
    return value


@dataclass(frozen=True)
class RegistryProfile:
    alias: str
    endpoint: str
    namespace: str = ""
    mirrors: tuple[str, ...] = ()
    ca: tuple[str, ...] = ()
    plain_http: bool = False
    tls_skip_verify: bool = False
    cache_from: tuple[str, ...] = ()
    cache_to: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        alias = _normalize_alias(self.alias)
        endpoint = normalize_endpoint(self.endpoint)
        namespace = "" if self.namespace == "" else _normalize_repository_path(self.namespace, "registry namespace")
        mirrors = tuple(_normalize_mirror(item) for item in _normalize_string_tuple(self.mirrors, "mirrors"))
        ca = _normalize_string_tuple(self.ca, "ca")
        normalized_ca: list[str] = []
        for item in ca:
            path = Path(item).expanduser()
            if not path.is_absolute():
                raise RegistryError("registry CA paths must be absolute")
            normalized_ca.append(os.fspath(path))
        if not isinstance(self.plain_http, bool) or not isinstance(self.tls_skip_verify, bool):
            raise RegistryError("plain_http and tls_skip_verify must be booleans")
        if self.plain_http and self.tls_skip_verify:
            raise RegistryError("plain_http and tls_skip_verify cannot both be enabled")
        if endpoint in mirrors:
            raise RegistryError("a registry cannot mirror itself")
        cache_from = tuple(
            validate_cache_spec(item, "cache_from") for item in _normalize_string_tuple(self.cache_from, "cache_from")
        )
        cache_to = tuple(
            validate_cache_spec(item, "cache_to") for item in _normalize_string_tuple(self.cache_to, "cache_to")
        )
        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "mirrors", mirrors)
        object.__setattr__(self, "ca", tuple(normalized_ca))
        object.__setattr__(self, "cache_from", cache_from)
        object.__setattr__(self, "cache_to", cache_to)


@dataclass(frozen=True)
class RegistryConfig:
    schema_version: int
    default: str
    registries: Mapping[str, RegistryProfile]

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != SCHEMA_VERSION:
            raise RegistryError(f"unsupported registry config schema_version: {self.schema_version!r}")
        default = _normalize_alias(self.default)
        if not isinstance(self.registries, Mapping):
            raise RegistryError("registries must be a table")
        profiles: dict[str, RegistryProfile] = {}
        for raw_alias, profile in self.registries.items():
            alias = _normalize_alias(raw_alias)
            if not isinstance(profile, RegistryProfile):
                raise RegistryError(f"registry profile {alias!r} is invalid")
            if profile.alias != alias:
                raise RegistryError(f"registry profile key {alias!r} does not match its alias")
            profiles[alias] = profile
        builtin = profiles.get(BUILTIN_ALIAS)
        if builtin is None:
            raise RegistryError("the built-in docker registry profile cannot be removed")
        if builtin.endpoint != BUILTIN_ENDPOINT or builtin.namespace != BUILTIN_NAMESPACE:
            raise RegistryError("the built-in docker profile must use docker.io and namespace library")
        if default not in profiles:
            raise RegistryError(f"default registry profile does not exist: {default!r}")
        object.__setattr__(self, "default", default)
        object.__setattr__(self, "registries", MappingProxyType(profiles))


def default_registry_config() -> RegistryConfig:
    profile = RegistryProfile(alias=BUILTIN_ALIAS, endpoint=BUILTIN_ENDPOINT, namespace=BUILTIN_NAMESPACE)
    return RegistryConfig(SCHEMA_VERSION, BUILTIN_ALIAS, {BUILTIN_ALIAS: profile})


def add_profile(config: RegistryConfig, profile: RegistryProfile, *, replace_existing: bool = False) -> RegistryConfig:
    """Purely return ``config`` with ``profile`` added or replaced."""
    if profile.alias in config.registries and not replace_existing:
        raise RegistryError(f"registry profile already exists: {profile.alias!r}")
    profiles = dict(config.registries)
    profiles[profile.alias] = profile
    return RegistryConfig(config.schema_version, config.default, profiles)


def remove_profile(config: RegistryConfig, alias: str) -> RegistryConfig:
    """Purely return ``config`` without ``alias``."""
    normalized = _normalize_alias(alias)
    if normalized == BUILTIN_ALIAS:
        raise RegistryError("the built-in docker registry profile cannot be removed")
    if normalized not in config.registries:
        raise RegistryError(f"unknown registry profile: {normalized!r}")
    profiles = dict(config.registries)
    del profiles[normalized]
    default = BUILTIN_ALIAS if config.default == normalized else config.default
    return RegistryConfig(config.schema_version, default, profiles)


def use_profile(config: RegistryConfig, alias: str) -> RegistryConfig:
    """Purely return ``config`` with ``alias`` selected as its default."""
    normalized = _normalize_alias(alias)
    if normalized not in config.registries:
        raise RegistryError(f"unknown registry profile: {normalized!r}")
    return replace(config, default=normalized)


def list_profiles(config: RegistryConfig) -> tuple[RegistryProfile, ...]:
    return tuple(config.registries[alias] for alias in sorted(config.registries))


def inspect_profile(config: RegistryConfig, alias: str | None = None) -> RegistryProfile:
    normalized = config.default if alias is None else _normalize_alias(alias)
    try:
        return config.registries[normalized]
    except KeyError as exc:
        raise RegistryError(f"unknown registry profile: {normalized!r}") from exc


def select_registry_alias(
    config: RegistryConfig,
    *,
    explicit_alias: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Select explicit alias, then ``PALIMPSEST_REGISTRY``, then config default."""
    env = os.environ if environment is None else environment
    candidate = explicit_alias if explicit_alias is not None else env.get("PALIMPSEST_REGISTRY", config.default)
    alias = _normalize_alias(candidate)
    if alias not in config.registries:
        raise RegistryError(f"unknown registry profile: {alias!r}")
    return alias


def select_registry_profile(
    config: RegistryConfig,
    *,
    explicit_alias: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> RegistryProfile:
    return config.registries[select_registry_alias(config, explicit_alias=explicit_alias, environment=environment)]


def registry_config_path(roots: StatePaths) -> Path:
    return roots.config / "registries.toml"


def registry_lock_path(roots: StatePaths) -> Path:
    return roots.locks / "registries.lock"


def resolve_docker_config_dir(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve Docker's existing credential/config directory without modifying it.

    Buildx and the regular Docker commands must share this directory.  In
    particular, Palimpsest must not silently create a second, empty credential
    store under its own XDG config root.
    """
    env = os.environ if environment is None else environment
    configured = env.get("DOCKER_CONFIG")
    if configured is not None:
        raw = _require_text(configured, "DOCKER_CONFIG")
        return Path(raw).expanduser().resolve(strict=False)
    home = env.get("HOME")
    base = Path(_require_text(home, "HOME")) if home is not None else Path.home()
    return (base.expanduser() / ".docker").resolve(strict=False)


def _reject_secret_tree(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                raise RegistryError("registries.toml must not contain credentials or secret-shaped fields")
            _reject_secret_tree(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_tree(item)
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise RegistryError("registries.toml must not contain inline credentials or secrets")


def _parse_registry_config(payload: bytes) -> RegistryConfig:
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError("invalid registries.toml") from exc
    _reject_secret_tree(raw)
    if set(raw) != _TOP_LEVEL_FIELDS:
        unknown = sorted(set(raw) - _TOP_LEVEL_FIELDS)
        missing = sorted(_TOP_LEVEL_FIELDS - set(raw))
        raise RegistryError(f"invalid registries.toml fields; unknown={unknown}, missing={missing}")
    tables = raw["registries"]
    if not isinstance(tables, dict):
        raise RegistryError("registries must be a TOML table")
    profiles: dict[str, RegistryProfile] = {}
    for alias, fields in tables.items():
        if not isinstance(fields, dict):
            raise RegistryError(f"registry profile {alias!r} must be a TOML table")
        if not set(fields).issubset(_PROFILE_FIELDS):
            raise RegistryError(f"registry profile {alias!r} contains unsupported fields")
        missing = {"endpoint"} - set(fields)
        if missing:
            raise RegistryError(f"registry profile {alias!r} is missing fields: {sorted(missing)}")
        profiles[alias] = RegistryProfile(alias=alias, **fields)
    return RegistryConfig(schema_version=raw["schema_version"], default=raw["default"], registries=profiles)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_registry_config(config: RegistryConfig) -> str:
    """Return deterministic, secret-free TOML for ``config``."""
    lines = [f"schema_version = {SCHEMA_VERSION}", f"default = {_toml_string(config.default)}", ""]
    for profile in list_profiles(config):
        lines.extend(
            [
                f"[registries.{profile.alias}]",
                f"endpoint = {_toml_string(profile.endpoint)}",
                f"namespace = {_toml_string(profile.namespace)}",
                f"mirrors = [{', '.join(_toml_string(item) for item in profile.mirrors)}]",
                f"ca = [{', '.join(_toml_string(item) for item in profile.ca)}]",
                f"plain_http = {'true' if profile.plain_http else 'false'}",
                f"tls_skip_verify = {'true' if profile.tls_skip_verify else 'false'}",
                f"cache_from = [{', '.join(_toml_string(item) for item in profile.cache_from)}]",
                f"cache_to = [{', '.join(_toml_string(item) for item in profile.cache_to)}]",
                "",
            ]
        )
    return "\n".join(lines)


def registry_config_digest(config: RegistryConfig) -> str:
    canonical = render_registry_config(config).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _validate_owner_only_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RegistryError(f"cannot inspect registry config: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RegistryError("registries.toml must be a regular file, not a symlink")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise RegistryError("registries.toml must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RegistryError("registries.toml permissions must be owner-only (0600)")


def _read_registry_config_unlocked(roots: StatePaths) -> RegistryConfig:
    path = registry_config_path(roots)
    if not path.exists():
        return default_registry_config()
    _validate_owner_only_file(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"cannot read registry config: {path}") from exc
    return _parse_registry_config(payload)


def load_registry_config(roots: StatePaths) -> RegistryConfig:
    return _read_registry_config_unlocked(roots)


def _write_registry_config_unlocked(roots: StatePaths, config: RegistryConfig) -> None:
    path = registry_config_path(roots)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    data = render_registry_config(config).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".registries-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def save_registry_config(roots: StatePaths, config: RegistryConfig) -> None:
    with file_lock(registry_lock_path(roots)):
        _write_registry_config_unlocked(roots, config)


def update_registry_config(
    roots: StatePaths,
    transform: Callable[[RegistryConfig], RegistryConfig],
) -> RegistryConfig:
    """Atomically read, transform, and replace the config under ``roots.locks``."""
    with file_lock(registry_lock_path(roots)):
        current = _read_registry_config_unlocked(roots)
        updated = transform(current)
        if not isinstance(updated, RegistryConfig):
            raise RegistryError("registry config transform must return RegistryConfig")
        _write_registry_config_unlocked(roots, updated)
        return updated


@dataclass(frozen=True)
class ResolvedImageReference:
    original: str
    endpoint: str
    repository: str
    tag: str | None
    digest: str | None
    registry_alias: str | None

    @property
    def name(self) -> str:
        return f"{self.endpoint}/{self.repository}"

    @property
    def canonical(self) -> str:
        value = self.name
        if self.tag is not None:
            value += f":{self.tag}"
        if self.digest is not None:
            value += f"@{self.digest}"
        return value

    @property
    def source_id(self) -> str:
        """Stable provenance identifier; a tag is not confused with a digest pin."""
        if self.digest is not None:
            tag_prefix = f"tag:{self.tag}," if self.tag is not None else ""
            return f"{tag_prefix}digest:{self.digest}"
        if self.tag is None:  # pragma: no cover - construction invariant
            raise RegistryError("resolved image reference has no source identifier")
        return f"tag:{self.tag}"

    def __str__(self) -> str:
        return self.canonical


def _has_explicit_registry(name: str) -> bool:
    if "/" not in name:
        return False
    first = name.split("/", 1)[0]
    return "." in first or ":" in first or first.lower() == "localhost" or first.startswith("[")


def _profile_alias_for_endpoint(config: RegistryConfig, endpoint: str) -> str | None:
    matches = sorted(profile.alias for profile in config.registries.values() if profile.endpoint == endpoint)
    if config.default in matches:
        return config.default
    return matches[0] if matches else None


def resolve_image_reference(
    reference: str,
    config: RegistryConfig,
    *,
    registry_alias: str | None = None,
    environment: Mapping[str, str] | None = None,
    require_digest: bool = False,
    default_tag: bool = True,
) -> ResolvedImageReference:
    """Resolve a Docker-compatible image reference to an explicit registry.

    An explicit registry in ``reference`` wins over ``registry_alias``, the
    environment, and the configured default.  Otherwise selection order is
    explicit alias, ``PALIMPSEST_REGISTRY``, then ``config.default``.
    """
    original = _require_text(reference, "image reference", maximum=512)
    if original.count("@") > 1:
        raise RegistryError("image reference contains multiple digest separators")
    name_and_tag, separator, digest_text = original.partition("@")
    digest: str | None = None
    if separator:
        if _PINNED_DIGEST_RE.fullmatch(digest_text) is None:
            raise RegistryError("image digest pins must use sha256:<64 hex characters>")
        digest = normalize_digest(digest_text)
    if require_digest and digest is None:
        raise RegistryError("this operation requires a digest-pinned image reference")

    last_slash = name_and_tag.rfind("/")
    last_colon = name_and_tag.rfind(":")
    tag: str | None = None
    name = name_and_tag
    if last_colon > last_slash:
        name, tag = name_and_tag[:last_colon], name_and_tag[last_colon + 1 :]
        if _TAG_RE.fullmatch(tag) is None:
            raise RegistryError("invalid Docker image tag")
    if not name:
        raise RegistryError("image reference is missing a repository name")

    if _has_explicit_registry(name):
        endpoint_text, repository = name.split("/", 1)
        endpoint = normalize_endpoint(endpoint_text)
        repository = _normalize_repository_path(repository, "image repository")
        selected_alias = _profile_alias_for_endpoint(config, endpoint)
        if endpoint == BUILTIN_ENDPOINT and "/" not in repository:
            repository = f"{BUILTIN_NAMESPACE}/{repository}"
    else:
        profile = select_registry_profile(
            config,
            explicit_alias=registry_alias,
            environment=environment,
        )
        endpoint = profile.endpoint
        repository = _normalize_repository_path(name, "image repository")
        if "/" not in repository and profile.namespace:
            repository = f"{profile.namespace}/{repository}"
        selected_alias = profile.alias

    if tag is None and digest is None and default_tag:
        tag = "latest"
    return ResolvedImageReference(original, endpoint, repository, tag, digest, selected_alias)


def _argv_text(value: os.PathLike[str] | str, field: str = "Docker argument") -> str:
    text = os.fspath(value)
    if not text or "\x00" in text:
        raise RegistryError(f"{field} must be a nonempty NUL-free string")
    return text


def docker_command_argv(
    config_dir: os.PathLike[str] | str,
    *arguments: os.PathLike[str] | str,
) -> list[str]:
    """Build arbitrary Docker CLI argv with an explicit credential/config directory."""
    directory = Path(config_dir).expanduser().resolve()
    if not arguments:
        raise RegistryError("Docker command requires a subcommand")
    command = ["docker", "--config", os.fspath(directory), *(_argv_text(item) for item in arguments)]
    subcommand_index = _docker_subcommand_index(command)
    if subcommand_index is not None and any(
        item == "--config" or item.startswith("--config=") for item in command[3:subcommand_index]
    ):
        raise RegistryError("set DOCKER_CONFIG instead of overriding Docker's global --config option")
    _reject_docker_login_password_argv(command)
    return command


def _docker_subcommand_index(command: Sequence[str]) -> int | None:
    index = 3
    while index < len(command):
        token = command[index]
        if token == "--":
            return index + 1 if index + 1 < len(command) else None
        if not token.startswith("-") or token == "-":
            return index
        option = token.split("=", 1)[0]
        if option in _DOCKER_GLOBAL_OPTIONS_WITH_VALUE and "=" not in token:
            index += 2
        else:
            index += 1
    return None


def _reject_docker_login_password_argv(command: Sequence[str]) -> None:
    subcommand_index = _docker_subcommand_index(command)
    if subcommand_index is None or command[subcommand_index] != "login":
        return
    for item in command[subcommand_index + 1 :]:
        if item == "--password" or item.startswith("--password=") or item.startswith("-p"):
            raise RegistryError("Docker login passwords are accepted only through --password-stdin")


def docker_login_argv(
    config_dir: os.PathLike[str] | str,
    endpoint: str,
    *,
    username: str | None = None,
    password_stdin: bool = False,
) -> list[str]:
    arguments = ["login"]
    if username is not None:
        arguments.extend(["--username", _require_text(username, "Docker username", maximum=255)])
    if password_stdin:
        arguments.append("--password-stdin")
    arguments.append(normalize_endpoint(endpoint))
    return docker_command_argv(config_dir, *arguments)


def docker_logout_argv(config_dir: os.PathLike[str] | str, endpoint: str) -> list[str]:
    return docker_command_argv(config_dir, "logout", normalize_endpoint(endpoint))


def docker_pull_argv(
    config_dir: os.PathLike[str] | str,
    reference: str,
    *,
    platform: str | None = None,
    all_tags: bool = False,
    quiet: bool = False,
) -> list[str]:
    arguments = ["pull"]
    if platform is not None:
        arguments.extend(["--platform", _require_text(platform, "platform", maximum=128)])
    if all_tags:
        arguments.append("--all-tags")
    if quiet:
        arguments.append("--quiet")
    arguments.append(_argv_text(reference, "image reference"))
    return docker_command_argv(config_dir, *arguments)


def docker_push_argv(
    config_dir: os.PathLike[str] | str,
    reference: str,
    *,
    platform: str | None = None,
    all_tags: bool = False,
    quiet: bool = False,
) -> list[str]:
    arguments = ["push"]
    if platform is not None:
        arguments.extend(["--platform", _require_text(platform, "platform", maximum=128)])
    if all_tags:
        arguments.append("--all-tags")
    if quiet:
        arguments.append("--quiet")
    arguments.append(_argv_text(reference, "image reference"))
    return docker_command_argv(config_dir, *arguments)


def docker_tag_argv(config_dir: os.PathLike[str] | str, source: str, target: str) -> list[str]:
    return docker_command_argv(
        config_dir,
        "tag",
        _argv_text(source, "source image"),
        _argv_text(target, "target image"),
    )


def docker_images_argv(
    config_dir: os.PathLike[str] | str,
    repository: str | None = None,
    *,
    all_images: bool = False,
    digests: bool = False,
    quiet: bool = False,
    no_trunc: bool = False,
    tree: bool = False,
    filters: Sequence[str] = (),
    output_format: str | None = None,
) -> list[str]:
    arguments = ["images"]
    if all_images:
        arguments.append("--all")
    if digests:
        arguments.append("--digests")
    if quiet:
        arguments.append("--quiet")
    if no_trunc:
        arguments.append("--no-trunc")
    if tree:
        arguments.append("--tree")
    for item in filters:
        arguments.extend(["--filter", _argv_text(item, "image filter")])
    if output_format is not None:
        arguments.extend(["--format", _argv_text(output_format, "image output format")])
    if repository is not None:
        arguments.append(_argv_text(repository, "image repository"))
    return docker_command_argv(config_dir, *arguments)


def docker_image_inspect_argv(
    config_dir: os.PathLike[str] | str,
    references: Sequence[str],
    *,
    platform: str | None = None,
    output_format: str | None = None,
) -> list[str]:
    if not references:
        raise RegistryError("docker image inspect requires at least one image")
    arguments = ["image", "inspect"]
    if platform is not None:
        arguments.extend(["--platform", _require_text(platform, "platform", maximum=128)])
    if output_format is not None:
        arguments.extend(["--format", _argv_text(output_format, "inspect output format")])
    arguments.extend(_argv_text(item, "image reference") for item in references)
    return docker_command_argv(config_dir, *arguments)


def docker_image_rm_argv(
    config_dir: os.PathLike[str] | str,
    references: Sequence[str],
    *,
    force: bool = False,
    no_prune: bool = False,
    platforms: Sequence[str] = (),
) -> list[str]:
    if not references:
        raise RegistryError("docker image rm requires at least one image")
    arguments = ["image", "rm"]
    if force:
        arguments.append("--force")
    if no_prune:
        arguments.append("--no-prune")
    for platform in platforms:
        arguments.extend(["--platform", _require_text(platform, "platform", maximum=128)])
    arguments.extend(_argv_text(item, "image reference") for item in references)
    return docker_command_argv(config_dir, *arguments)


def docker_history_argv(
    config_dir: os.PathLike[str] | str,
    reference: str,
    *,
    human: bool = True,
    no_trunc: bool = False,
    quiet: bool = False,
    output_format: str | None = None,
    platform: str | None = None,
) -> list[str]:
    arguments = ["image", "history"]
    if not human:
        arguments.extend(["--human", "false"])
    if no_trunc:
        arguments.append("--no-trunc")
    if quiet:
        arguments.append("--quiet")
    if output_format is not None:
        arguments.extend(["--format", _argv_text(output_format, "history output format")])
    if platform is not None:
        arguments.extend(["--platform", _require_text(platform, "platform", maximum=128)])
    arguments.append(_argv_text(reference, "image reference"))
    return docker_command_argv(config_dir, *arguments)


def docker_save_argv(
    config_dir: os.PathLike[str] | str,
    references: Sequence[str],
    *,
    output: os.PathLike[str] | str | None = None,
    platforms: Sequence[str] = (),
) -> list[str]:
    if not references:
        raise RegistryError("docker image save requires at least one image")
    arguments = ["image", "save"]
    if output is not None:
        arguments.extend(["--output", os.fspath(Path(output).expanduser().resolve())])
    for platform in platforms:
        arguments.extend(["--platform", _require_text(platform, "platform", maximum=128)])
    arguments.extend(_argv_text(item, "image reference") for item in references)
    return docker_command_argv(config_dir, *arguments)


def docker_load_argv(
    config_dir: os.PathLike[str] | str,
    *,
    input_path: os.PathLike[str] | str | None = None,
    platforms: Sequence[str] = (),
    quiet: bool = False,
) -> list[str]:
    arguments = ["image", "load"]
    if input_path is not None:
        arguments.extend(["--input", os.fspath(Path(input_path).expanduser().resolve())])
    for platform in platforms:
        arguments.extend(["--platform", _require_text(platform, "platform", maximum=128)])
    if quiet:
        arguments.append("--quiet")
    return docker_command_argv(config_dir, *arguments)


def run_docker_command(
    argv: Sequence[str],
    *,
    stdin_text: str | None = None,
    timeout_seconds: float | None = 600,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Run validated Docker argv directly, never through a shell."""
    command = [_argv_text(item) for item in argv]
    if len(command) < 4 or command[0] != "docker" or command[1] != "--config":
        raise RegistryError("Docker argv must start with 'docker --config DIR'")
    _reject_docker_login_password_argv(command)
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "check": False,
        "shell": False,
    }
    if stdin_text is not None:
        if not isinstance(stdin_text, str):
            raise RegistryError("Docker stdin must be text")
        kwargs["input"] = stdin_text
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise RegistryError("Docker command timeout must be positive")
        kwargs["timeout"] = timeout_seconds
    try:
        result = runner(command, **kwargs)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise RegistryError(f"Docker command failed to start or timed out: {command[3]}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[-4000:]
        detail = f": {stderr}" if stderr else ""
        raise RegistryError(f"Docker {command[3]} failed with exit code {result.returncode}{detail}")
    return result


def run_docker_passthrough(
    argv: Sequence[str],
    *,
    timeout_seconds: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> subprocess.CompletedProcess[Any]:
    """Run Docker with inherited stdin/stdout/stderr for progress or login UX.

    The caller receives Docker's nonzero exit status unchanged.  This is useful
    for interactive/device-code login and progress streams, where capturing
    output would hide prompts.  ``--password``/``-p`` remain forbidden, while a
    caller-supplied ``--password-stdin`` reads directly from the inherited stdin.
    """
    command = [_argv_text(item) for item in argv]
    if len(command) < 4 or command[0] != "docker" or command[1] != "--config":
        raise RegistryError("Docker argv must start with 'docker --config DIR'")
    _reject_docker_login_password_argv(command)
    kwargs: dict[str, Any] = {"shell": False, "check": False}
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise RegistryError("Docker command timeout must be positive")
        kwargs["timeout"] = timeout_seconds
    try:
        return runner(command, **kwargs)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise RegistryError(f"Docker command failed to start or timed out: {command[3]}") from exc


def run_docker_login(
    config_dir: os.PathLike[str] | str,
    endpoint: str,
    password: str,
    *,
    username: str | None = None,
    timeout_seconds: float | None = 600,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Authenticate using stdin; ``password`` is never included in argv."""
    if not isinstance(password, str) or not password or "\x00" in password or "\r" in password or "\n" in password:
        raise RegistryError("Docker password must be nonempty single-line text")
    argv = docker_login_argv(config_dir, endpoint, username=username, password_stdin=True)
    return run_docker_command(argv, stdin_text=f"{password}\n", timeout_seconds=timeout_seconds, runner=runner)


def render_buildkitd_toml(config: RegistryConfig) -> str:
    """Render deterministic BuildKit registry mirror and TLS configuration."""
    endpoints: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool, bool]] = {}
    for profile in list_profiles(config):
        settings = (profile.mirrors, profile.ca, profile.plain_http, profile.tls_skip_verify)
        existing = endpoints.get(profile.endpoint)
        if existing is not None and existing != settings:
            raise RegistryError(
                f"profiles for endpoint {profile.endpoint!r} have conflicting BuildKit transport settings"
            )
        endpoints[profile.endpoint] = settings

    lines = ["# Generated by Palimpsest. Credentials are intentionally excluded.", ""]
    for endpoint in sorted(endpoints):
        mirrors, ca, plain_http, tls_skip_verify = endpoints[endpoint]
        lines.append(f"[registry.{_toml_string(endpoint)}]")
        if mirrors:
            lines.append(f"  mirrors = [{', '.join(_toml_string(item) for item in mirrors)}]")
        if ca:
            lines.append(f"  ca = [{', '.join(_toml_string(item) for item in ca)}]")
        if plain_http:
            lines.append("  http = true")
        if tls_skip_verify:
            lines.append("  insecure = true")
        lines.append("")
    return "\n".join(lines)
