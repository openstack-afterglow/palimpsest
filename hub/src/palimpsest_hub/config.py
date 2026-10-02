from __future__ import annotations

import hashlib
import ipaddress
import re
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_keystone_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None:
        raise ValueError("Keystone identity must be an exact bounded ASCII identifier")
    return value


def default_project_namespace(project_id: str) -> str:
    project_id = validate_keystone_id(project_id)
    if re.fullmatch(r"[0-9a-f]{32}", project_id):
        return f"p-{project_id}"
    return "p-h-" + hashlib.sha256(project_id.encode("ascii")).hexdigest()[:56]


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    database_url: str
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=100)
    database_connect_timeout: int = Field(default=10, ge=1, le=120)
    database_pool_timeout: int = Field(default=30, ge=1, le=120)
    database_unhealthy_seconds: int = Field(default=30, ge=1, le=3600)


class Settings(DatabaseSettings):
    redis_url: str

    palimpsest_hub_local_path: str
    palimpsest_hub_max_blob_bytes: int = Field(default=107374182400, ge=1)
    palimpsest_hub_max_bundle_expanded_bytes: int = Field(default=107374182400, ge=1)
    palimpsest_hub_max_blocking_operations: int = Field(default=2, ge=1, le=16)
    # A separate KVM-capable worker installs palimpsest-local in this interpreter.
    # Unset keeps remote builds disabled; the API container never runs builds.
    palimpsest_hub_builder_python: str = ""
    palimpsest_hub_build_timeout_seconds: int = Field(default=3600, ge=60, le=3600)
    palimpsest_hub_package_forbidden_project_ids: tuple[str, ...] = ()
    palimpsest_hub_package_forbidden_user_ids: tuple[str, ...] = ()
    palimpsest_hub_package_namespace_bindings: dict[str, str] = Field(default_factory=dict)
    palimpsest_hub_package_public_origin: str = ""

    os_auth_url: str
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

    @field_validator("palimpsest_hub_package_forbidden_project_ids", "palimpsest_hub_package_forbidden_user_ids")
    @classmethod
    def canonical_protected_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(validate_keystone_id(item) for item in value))

    @field_validator("palimpsest_hub_package_namespace_bindings")
    @classmethod
    def canonical_namespace_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        projects: set[str] = set()
        for namespace, project_id in value.items():
            if len(namespace) > 63 or re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*", namespace) is None:
                raise ValueError("package namespace must be a canonical lower-case repository component")
            project = validate_keystone_id(project_id)
            if re.fullmatch(r"p-[0-9a-f]{32}|p-h-[0-9a-f]{56}", namespace) and namespace != default_project_namespace(project):
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


class BuildWorkerSettings(DatabaseSettings):
    """The KVM host needs SQL and blob access, never Keystone or Redis secrets."""

    palimpsest_hub_local_path: str
    palimpsest_hub_max_blob_bytes: int = Field(default=107374182400, ge=1)
    palimpsest_hub_builder_python: str
    palimpsest_hub_build_timeout_seconds: int = Field(default=3600, ge=60, le=3600)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_build_worker_settings() -> BuildWorkerSettings:
    return BuildWorkerSettings()
