from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio.connection import SSLConnection, parse_url

# Never delegated through an export Trust, whatever the operator configures.
FORBIDDEN_DELEGATED_ROLES = frozenset({"admin", "manager", "service"})


def validate_keystone_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None:
        raise ValueError("Keystone identity must be an exact bounded ASCII identifier")
    return value


def default_project_namespace(project_id: str) -> str:
    project_id = validate_keystone_id(project_id)
    if re.fullmatch(r"[0-9a-f]{32}", project_id):
        return f"p-{project_id}"
    return "p-h-" + hashlib.sha256(project_id.encode("ascii")).hexdigest()[:56]


DEFAULT_REGISTRY_HOSTS = ("docker.io", "registry-1.docker.io", "ghcr.io", "quay.io")


def validate_registry_hosts(values: tuple[str, ...]) -> tuple[str, ...]:
    """Operators explicitly select registry authorities, never caller-supplied URLs."""
    if not 1 <= len(values) <= 64 or len(set(values)) != len(values):
        raise ValueError("registry authorities must be a distinct list of 1..64 hosts")
    for value in values:
        parsed = urlsplit("https://" + value)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("registry authority port is invalid") from None
        if (
            value != value.lower()
            or len(value) > 259
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
            or (port is not None and not 1 <= port <= 65535)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?", value)
            or any(not label or len(label) > 63 for label in parsed.hostname.split("."))
        ):
            raise ValueError("registry authorities must be explicit lowercase hostname[:port] values")
    return values


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    database_url: str
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=100)
    database_connect_timeout: int = Field(default=10, ge=1, le=120)
    database_read_timeout: int = Field(default=30, ge=1, le=120)
    database_pool_timeout: int = Field(default=30, ge=1, le=120)
    database_unhealthy_seconds: int = Field(default=30, ge=1, le=3600)


class InstanceGpuProfile(BaseModel):
    """Operator-qualified guest/PCI tuple, never caller-selected host paths."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    image_id: str
    flavor_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    pci_alias: str = Field(min_length=3, max_length=575)
    driver_version: str = Field(pattern=r"^[0-9]{3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$")
    device_count: int = Field(ge=1, le=16)
    project_ids: list[str] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def exact_gpu_tuple(self) -> InstanceGpuProfile:
        if str(UUID(self.image_id)) != self.image_id:
            raise ValueError("GPU guest image must be a canonical UUID")
        aliases = self.pci_alias.split(",")
        if (
            len(aliases) > 8
            or any(re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}:(?:[1-9]|1[0-6])", item) is None for item in aliases)
            or len({item.split(":", 1)[0] for item in aliases}) != len(aliases)
        ):
            raise ValueError("GPU PCI aliases must be distinct exact alias:count pairs")
        if len(set(self.project_ids)) != len(self.project_ids) or any(
            UUID(identity).hex != identity for identity in self.project_ids
        ):
            raise ValueError("GPU project_ids must be distinct canonical OpenStack UUID hex identifiers")
        return self


class InstanceClusterProfile(BaseModel):
    """Explicit whole-VM root privilege qualification for the fixed k3s runtime."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    image_id: str
    flavor_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    project_ids: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("flavor_ids", "project_ids", mode="before")
    @classmethod
    def json_sequences(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def exact_cluster_tuple(self) -> InstanceClusterProfile:
        if str(UUID(self.image_id)) != self.image_id:
            raise ValueError("Cluster guest image must be a canonical UUID")
        if len(set(self.flavor_ids)) != len(self.flavor_ids) or any(
            re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", item) is None for item in self.flavor_ids
        ):
            raise ValueError("Cluster flavors must be distinct bounded identifiers")
        if len(set(self.project_ids)) != len(self.project_ids) or any(
            UUID(item).hex != item for item in self.project_ids
        ):
            raise ValueError("Cluster projects must be distinct canonical OpenStack UUID hex identifiers")
        return self


class Settings(DatabaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", hide_input_in_errors=True)

    redis_url: str
    redis_sentinel_addresses: tuple[tuple[str, int], ...] = ()
    redis_sentinel_master: str = ""
    redis_sentinel_password: SecretStr = SecretStr("")
    oslo_policy_policy_file: str = "/etc/palimpsest/policy.yaml"
    oslo_policy_policy_dirs: tuple[str, ...] = ()

    palimpsest_hub_local_path: str
    palimpsest_hub_max_blob_bytes: int = Field(default=107374182400, ge=1)
    palimpsest_hub_max_bundle_expanded_bytes: int = Field(default=107374182400, ge=1)
    palimpsest_hub_max_blocking_operations: int = Field(default=2, ge=1, le=16)
    # The control plane admits remote builds only when a worker secret is configured.
    palimpsest_hub_build_worker_key: SecretStr = SecretStr("")
    palimpsest_hub_build_timeout_seconds: int = Field(default=3600, ge=60, le=3600)
    palimpsest_hub_registry_hosts: tuple[str, ...] = DEFAULT_REGISTRY_HOSTS
    palimpsest_hub_image_run_network: Literal["none", "default"] = "none"
    palimpsest_hub_image_cgroup_root: str = "/sys/fs/cgroup"
    palimpsest_hub_max_context_bytes: int = Field(default=16 * 1024**2, ge=1, le=1024**3)
    palimpsest_hub_max_context_expanded_bytes: int = Field(default=256 * 1024**2, ge=1, le=32 * 1024**3)
    palimpsest_hub_max_context_members: int = Field(default=10000, ge=1, le=250000)
    palimpsest_hub_instance_bootstrap_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    palimpsest_hub_max_instances_per_project: int = Field(default=16, ge=1, le=1000)
    palimpsest_hub_instance_max_boot_gib: int = Field(default=256, ge=1, le=4096)
    palimpsest_hub_instance_max_writable_gib: int = Field(default=64, ge=1, le=1024)
    # Operators may qualify an unannotated Glance image only by exact content hash.
    palimpsest_hub_instance_base_images: dict[str, dict[str, str]] = Field(default_factory=dict)
    palimpsest_hub_instance_gpu_profiles: dict[str, InstanceGpuProfile] = Field(default_factory=dict)
    palimpsest_hub_instance_cluster_profiles: dict[str, InstanceClusterProfile] = Field(default_factory=dict)
    palimpsest_hub_instance_ca_certificates: tuple[str, ...] = ()
    palimpsest_hub_package_forbidden_project_ids: tuple[str, ...] = ()
    palimpsest_hub_package_forbidden_user_ids: tuple[str, ...] = ()
    palimpsest_hub_package_namespace_bindings: dict[str, str] = Field(default_factory=dict)
    palimpsest_hub_package_public_origin: str = ""
    # Deferred Glance exports run only through a requester-created Keystone Trust
    # with these least global roles and this finite lifetime. Glance's default
    # `download_image` policy requires `member` even for project-owned images.
    palimpsest_hub_export_delegated_roles: tuple[str, ...] = ("member",)
    palimpsest_hub_export_delegation_ttl_seconds: int = Field(default=21600, ge=900, le=86400)

    os_auth_url: str
    # Export Trust trustee. It authenticates only to its own service project; it is
    # never scoped to a tenant project and holds no tenant role assignments.
    os_username: str
    os_password: SecretStr
    os_project_name: str
    os_user_domain_name: str = "Default"
    os_project_domain_name: str = "Default"
    os_region_name: str = "RegionOne"
    os_interface: str = "internal"
    ssl_verify: bool = True
    # Token validation never falls back to the Glance service identity above.
    os_reader_username: str = ""
    os_reader_password: SecretStr = SecretStr("")
    os_reader_user_domain_name: str = "Default"

    @field_validator("palimpsest_hub_registry_hosts")
    @classmethod
    def registry_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_registry_hosts(value)

    @field_validator("palimpsest_hub_instance_base_images")
    @classmethod
    def qualified_base_images(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        if len(value) > 64:
            raise ValueError("at most 64 content-pinned instance base images are allowed")
        for identity, record in value.items():
            if str(UUID(identity)) != identity or set(record) != {
                "architecture",
                "os_type",
                "os_hash_algo",
                "os_hash_value",
            }:
                raise ValueError("qualified base image requires UUID, architecture, Linux and immutable Glance hash")
            algorithm = record["os_hash_algo"]
            size = {"sha256": 64, "sha512": 128}.get(algorithm)
            if (
                record["architecture"] != "x86_64"
                or record["os_type"] != "linux"
                or size is None
                or re.fullmatch(r"[0-9a-f]{" + str(size) + r"}", record["os_hash_value"]) is None
            ):
                raise ValueError("qualified base image content and target are invalid")
        return value

    @field_validator("palimpsest_hub_instance_gpu_profiles")
    @classmethod
    def qualified_gpu_profiles(cls, value: dict[str, InstanceGpuProfile]) -> dict[str, InstanceGpuProfile]:
        if len(value) > 64 or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key) is None for key in value):
            raise ValueError("GPU qualification requires at most 64 bounded profile identifiers")
        return value

    @field_validator("palimpsest_hub_instance_cluster_profiles")
    @classmethod
    def qualified_cluster_profiles(cls, value: dict[str, InstanceClusterProfile]) -> dict[str, InstanceClusterProfile]:
        if len(value) > 64 or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key) is None for key in value):
            raise ValueError("Cluster qualification requires at most 64 bounded profile identifiers")
        return value

    @model_validator(mode="after")
    def cluster_images_are_content_pinned(self) -> Settings:
        if any(
            profile.image_id not in self.palimpsest_hub_instance_base_images
            for profile in self.palimpsest_hub_instance_cluster_profiles.values()
        ):
            raise ValueError("Every cluster profile requires an exact content-pinned instance base image")
        return self

    @model_validator(mode="after")
    def gpu_images_are_content_pinned(self) -> Settings:
        if any(
            profile.image_id not in self.palimpsest_hub_instance_base_images
            for profile in self.palimpsest_hub_instance_gpu_profiles.values()
        ):
            raise ValueError("Every GPU profile requires an exact content-pinned instance base image")
        return self

    @field_validator("palimpsest_hub_instance_ca_certificates")
    @classmethod
    def instance_ca_certificates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 4 or any(
            len(cert.encode()) > 16384
            or not cert.startswith("-----BEGIN CERTIFICATE-----\n")
            or not cert.rstrip().endswith("-----END CERTIFICATE-----")
            for cert in value
        ):
            raise ValueError("instance trust requires at most four bounded PEM CA certificates")
        return value

    @field_validator("palimpsest_hub_image_cgroup_root")
    @classmethod
    def image_cgroup_root(cls, value: str) -> str:
        path = Path(value)
        root = Path("/sys/fs/cgroup")
        if "\x00" in value or ".." in path.parts or not (path == root or root in path.parents):
            raise ValueError("image cgroup root must stay within the worker cgroup namespace")
        return value

    @field_validator("redis_sentinel_addresses")
    @classmethod
    def bounded_sentinel_addresses(cls, value: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
        if len(value) > 16:
            raise ValueError("Redis Sentinel supports at most 16 addresses")
        if len(set(value)) != len(value):
            raise ValueError("Redis Sentinel addresses must be distinct")
        for host, port in value:
            if (
                not host
                or len(host) > 253
                or any(character.isspace() or ord(character) < 32 for character in host)
                or any(character in host for character in "/@?#[]")
                or not 1 <= port <= 65535
            ):
                raise ValueError("Redis Sentinel addresses require a hostname/IP and port 1..65535")
            if ":" in host:
                try:
                    ipaddress.IPv6Address(host)
                except ValueError:
                    raise ValueError("Redis Sentinel IPv6 addresses must be bare IP literals") from None
        return value

    @model_validator(mode="after")
    def coherent_redis_sentinel(self) -> Settings:
        if not (
            self.redis_sentinel_addresses
            or self.redis_sentinel_master
            or self.redis_sentinel_password.get_secret_value()
        ):
            return self
        if not self.redis_sentinel_addresses or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", self.redis_sentinel_master):
            raise ValueError("Redis Sentinel requires addresses and a bounded ASCII master name")
        try:
            scheme = urlsplit(self.redis_url).scheme
            options = parse_url(self.redis_url)
        except (TypeError, ValueError):
            raise ValueError("Redis Sentinel requires a valid redis:// or rediss:// data URL") from None
        if scheme not in {"redis", "rediss"}:
            raise ValueError("Redis Sentinel requires a redis:// or rediss:// data URL")
        if options.get("connection_class") not in (None, SSLConnection):
            raise ValueError("Redis Sentinel cannot override its managed connection class")
        for name in ("socket_connect_timeout", "socket_timeout"):
            timeout = options.get(name, 2.0)
            if not math.isfinite(timeout) or not 0 < timeout <= 30:
                raise ValueError("Redis Sentinel socket timeouts must be finite and in (0, 30] seconds")
        return self

    @field_validator("palimpsest_hub_build_worker_key")
    @classmethod
    def bounded_worker_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret and (not 32 <= len(secret) <= 4096 or any(not 33 <= ord(char) <= 126 for char in secret)):
            raise ValueError("worker key must be 32..4096 printable non-whitespace ASCII characters")
        return value

    @field_validator("palimpsest_hub_package_forbidden_project_ids", "palimpsest_hub_package_forbidden_user_ids")
    @classmethod
    def canonical_protected_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(validate_keystone_id(item) for item in value))

    @field_validator("palimpsest_hub_export_delegated_roles")
    @classmethod
    def least_delegated_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        roles = tuple(dict.fromkeys(value))
        if (
            not roles
            or any(not isinstance(role, str) or not role or role != role.strip() or len(role) > 255 for role in roles)
            or {role.casefold() for role in roles} & FORBIDDEN_DELEGATED_ROLES
        ):
            raise ValueError("delegated export roles must be non-administrative Keystone role names")
        return roles

    @field_validator("palimpsest_hub_package_namespace_bindings")
    @classmethod
    def canonical_namespace_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        projects: set[str] = set()
        for namespace, project_id in value.items():
            if len(namespace) > 63 or re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*", namespace) is None:
                raise ValueError("package namespace must be a canonical lower-case repository component")
            project = validate_keystone_id(project_id)
            if re.fullmatch(r"p-[0-9a-f]{32}|p-h-[0-9a-f]{56}", namespace) and namespace != default_project_namespace(
                project
            ):
                raise ValueError("Reserved namespace cannot be bound to a different project")
            if project in projects:
                raise ValueError("a project can have only one configured package namespace")
            projects.add(project)
            result[namespace] = project
        return result

    @field_validator("palimpsest_hub_package_public_origin")
    @classmethod
    def trusted_package_origin(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if (
            value != value.strip()
            or any(character.isspace() for character in value)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("package public origin must be a trusted HTTPS origin without credentials or path")
        host = parsed.hostname
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        elif (
            len(host) > 253
            or host.endswith(".")
            or any(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in host.split("."))
        ):
            raise ValueError("package public origin hostname is invalid")
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("package public origin port is invalid")
        canonical = "https://" + host + (f":{port}" if port is not None else "")
        if value.rstrip("/") != canonical:
            raise ValueError("package public origin must use canonical lower-case authority and port")
        return canonical


class BuildWorkerSettings(BaseSettings):
    """A remote executor has HTTPS access and local scratch, never control-plane credentials."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", hide_input_in_errors=True)

    palimpsest_hub_url: str
    palimpsest_hub_build_worker_key_file: str
    palimpsest_hub_build_work_path: str
    palimpsest_hub_build_timeout_seconds: int = Field(default=3600, ge=60, le=3600)
    palimpsest_hub_max_blob_bytes: int = Field(default=107374182400, ge=1)
    palimpsest_hub_registry_hosts: tuple[str, ...] = DEFAULT_REGISTRY_HOSTS
    palimpsest_hub_image_run_network: Literal["none", "default"] = "none"
    palimpsest_hub_image_cgroup_root: str = "/sys/fs/cgroup"
    palimpsest_hub_image_scratch_bytes: int = Field(default=4 * 1024**3, ge=256 * 1024**2, le=64 * 1024**3)
    palimpsest_hub_max_context_bytes: int = Field(default=16 * 1024**2, ge=1, le=1024**3)
    palimpsest_hub_max_context_expanded_bytes: int = Field(default=256 * 1024**2, ge=1, le=32 * 1024**3)
    palimpsest_hub_max_context_members: int = Field(default=10000, ge=1, le=250000)

    @field_validator("palimpsest_hub_registry_hosts")
    @classmethod
    def registry_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_registry_hosts(value)

    @field_validator("palimpsest_hub_image_cgroup_root")
    @classmethod
    def image_cgroup_root(cls, value: str) -> str:
        path = Path(value)
        root = Path("/sys/fs/cgroup")
        if "\x00" in value or ".." in path.parts or not (path == root or root in path.parents):
            raise ValueError("image cgroup root must stay within the worker cgroup namespace")
        return value

    @field_validator("palimpsest_hub_url")
    @classmethod
    def https_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("worker Hub URL must be an HTTPS origin") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
            or (port is not None and port < 1)
        ):
            raise ValueError("worker Hub URL must be an HTTPS origin")
        return value.rstrip("/")

    @field_validator("palimpsest_hub_build_worker_key_file", "palimpsest_hub_build_work_path")
    @classmethod
    def absolute_local_path(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\x00" in value:
            raise ValueError("worker paths must be absolute local paths")
        return value


@lru_cache
def get_settings() -> Settings:
    path = Path(os.environ.get("PALIMPSEST_CONFIG", "/etc/palimpsest/palimpsest.conf"))
    policy_options = tomllib.loads(path.read_text()).get("oslo_policy", {}) if path.is_file() else {}
    values = {
        "oslo_policy_" + name: value
        for name, value in policy_options.items()
        if name in {"policy_file", "policy_dirs"} and "OSLO_POLICY_" + name.upper() not in os.environ
    }
    return Settings(**values)


@lru_cache
def get_build_worker_settings() -> BuildWorkerSettings:
    return BuildWorkerSettings()
