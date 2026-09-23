from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    os_auth_url: str
    os_username: str
    os_password: SecretStr
    os_project_name: str
    os_user_domain_name: str = "Default"
    os_project_domain_name: str = "Default"
    os_region_name: str = "RegionOne"
    os_interface: str = "internal"
    ssl_verify: bool = True


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
