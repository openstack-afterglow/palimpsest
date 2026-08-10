from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    database_url: str
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=100)
    database_connect_timeout: int = Field(default=10, ge=1, le=120)
    database_pool_timeout: int = Field(default=30, ge=1, le=120)
    database_unhealthy_seconds: int = Field(default=30, ge=1, le=3600)
    redis_url: str

    palimpsest_hub_local_path: str
    palimpsest_hub_max_blob_bytes: int = Field(default=107374182400, ge=1)
    palimpsest_hub_signing_secret: SecretStr

    os_auth_url: str
    os_username: str
    os_password: SecretStr
    os_user_domain_name: str = "Default"
    os_project_domain_name: str = "Default"
    os_region_name: str = "RegionOne"
    os_interface: str = "internal"
    ssl_verify: bool = True

    @field_validator("palimpsest_hub_signing_secret")
    @classmethod
    def require_signing_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("PALIMPSEST_HUB_SIGNING_SECRET must be at least 32 bytes")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
