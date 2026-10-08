from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*", re.ASCII)
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", re.ASCII)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_ACTIONS = {"packages:inventory", "packages:read", "packages:write", "cache:read", "cache:write"}


def canonical_package(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 253
        or any(_COMPONENT.fullmatch(p) is None for p in value.split("/"))
    ):
        raise ValueError("package must contain canonical lower-case repository components")
    return value


def canonical_namespace(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 63 or _COMPONENT.fullmatch(value) is None:
        raise ValueError("namespace must be one canonical lower-case repository component")
    return value


def canonical_tag(value: str) -> str:
    if _TAG.fullmatch(value) is None:
        raise ValueError("invalid tag")
    return value


def canonical_digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("digest must be canonical sha256")
    return value


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KeyScope(StrictDTO):
    packages: list[str] | None = None
    all_packages: bool | None = None

    @model_validator(mode="after")
    def exact_scope(self):
        if self.model_fields_set == {"all_packages"} and self.all_packages is True:
            return self
        if self.model_fields_set != {"packages"} or self.packages is None or not 1 <= len(self.packages) <= 32:
            raise ValueError("choose packages (1–32 exact names) or all_packages=true")
        for name in self.packages:
            canonical_package(name)
        if len(set(self.packages)) != len(self.packages):
            raise ValueError("duplicate package scope")
        return self


class KeyCreate(StrictDTO):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    scope: KeyScope
    actions: list[str] = Field(min_length=1, max_length=5)
    expires_in_days: int = Field(default=30, ge=1, le=90)

    @field_validator("actions")
    @classmethod
    def permissions(cls, value):
        if len(set(value)) != len(value) or not set(value) <= _ACTIONS:
            raise ValueError("invalid or duplicate key actions")
        return value


class Provenance(StrictDTO):
    source_revision: str | None = Field(default=None, max_length=128)
    build_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def supplied_strings(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("provenance values must be strings")
        return self


class PackageUploadStart(StrictDTO):
    package_type: Literal["oci-image", "runtime-bundle"]
    tag: str
    root_digest: str
    archive_digest: str
    archive_size_bytes: int = Field(ge=1)
    expected_tag_digest: str | None
    provenance: Provenance = Field(default_factory=Provenance)

    _tag = field_validator("tag")(canonical_tag)
    _digests = field_validator("root_digest", "archive_digest")(canonical_digest)

    @field_validator("expected_tag_digest")
    @classmethod
    def expected(cls, value):
        return canonical_digest(value) if value is not None else None


class CachePartition(StrictDTO):
    build_key: str
    cache_scope: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,47}$")
    platform: str = Field(max_length=64, pattern=r"^linux/(?:amd64|arm64)(?:/[a-z0-9_.-]+)?$")
    builder_fingerprint: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:+/-]+$")
    _key = field_validator("build_key")(canonical_digest)


class CacheUploadStart(CachePartition):
    archive_digest: str
    archive_size_bytes: int = Field(ge=1)
    _digest = field_validator("archive_digest")(canonical_digest)


class EmptyBody(StrictDTO):
    pass
