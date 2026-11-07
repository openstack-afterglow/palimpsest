"""Administrator-selected upstream image sources, separate from registry auth."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from typing import Any

from .errors import StateError
from .registry import (
    RegistryConfig,
    RegistryError,
    ResolvedImageReference,
    default_registry_config,
    load_registry_config,
    normalize_endpoint,
    resolve_image_reference,
)
from .state import StatePaths, pinned_owner_directory

_POLICY_SCHEMA = 1
_PUBLISHER_POLICY = "registry-namespace"
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_RULES = 256


class ImageSourcePolicyError(RegistryError):
    """An upstream image source is outside the administrator's selected policy."""


@dataclass(frozen=True, slots=True)
class ImageSourceRule:
    registry: str
    namespace: str
    repository: str

    def __post_init__(self) -> None:
        if not isinstance(self.registry, str):
            raise ImageSourcePolicyError("image source registry must be a string")
        try:
            registry = normalize_endpoint(self.registry)
        except RegistryError:
            raise ImageSourcePolicyError("invalid image source registry") from None
        if (
            not isinstance(self.namespace, str)
            or "/" in self.namespace
            or not isinstance(self.repository, str)
            or not self.repository
            or (not self.namespace and "/" in self.repository)
        ):
            raise ImageSourcePolicyError("image sources require an exact namespace and repository")
        try:
            reference = resolve_image_reference(
                f"{registry}/{self.full_repository}",
                default_registry_config(),
                environment={},
                default_tag=False,
            )
        except RegistryError:
            raise ImageSourcePolicyError("invalid image source namespace or repository") from None
        expected = self.full_repository
        if reference.repository != expected or reference.tag is not None or reference.digest is not None:
            raise ImageSourcePolicyError("image source rules cannot contain tags, digests or wildcards")
        object.__setattr__(self, "registry", registry)

    @property
    def full_repository(self) -> str:
        return f"{self.namespace}/{self.repository}" if self.namespace else self.repository

    def to_dict(self) -> dict[str, str]:
        return {"registry": self.registry, "namespace": self.namespace, "repository": self.repository}


@dataclass(frozen=True, slots=True)
class ImageSourceAdmission:
    reference: ResolvedImageReference
    policy_digest: str
    publisher_namespace: str

    def to_dict(self) -> dict[str, str]:
        return {
            "reference": self.reference.canonical,
            "registry": self.reference.endpoint,
            "repository": self.reference.repository,
            "policy_digest": self.policy_digest,
            "publisher_namespace": self.publisher_namespace,
            "publisher_policy": _PUBLISHER_POLICY,
        }


@dataclass(frozen=True, slots=True)
class ImageSourcePolicy:
    rules: tuple[ImageSourceRule, ...] = ()
    publisher_policy: str = _PUBLISHER_POLICY

    def __post_init__(self) -> None:
        if self.publisher_policy != _PUBLISHER_POLICY:
            raise ImageSourcePolicyError("unsupported image publisher policy")
        if (
            not isinstance(self.rules, tuple)
            or len(self.rules) > _MAX_RULES
            or any(not isinstance(rule, ImageSourceRule) for rule in self.rules)
        ):
            raise ImageSourcePolicyError("image source rules must be a bounded immutable rule list")
        ordered = tuple(sorted(self.rules, key=lambda rule: (rule.registry, rule.namespace, rule.repository)))
        if len(set(ordered)) != len(ordered):
            raise ImageSourcePolicyError("duplicate image source rule")
        object.__setattr__(self, "rules", ordered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _POLICY_SCHEMA,
            "publisher_policy": self.publisher_policy,
            "rules": [rule.to_dict() for rule in self.rules],
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def admit(self, reference: ResolvedImageReference) -> ImageSourceAdmission:
        if not isinstance(reference, ResolvedImageReference):
            raise ImageSourcePolicyError("image admission requires an existing normalized registry reference")
        try:
            canonical = resolve_image_reference(
                reference.canonical, default_registry_config(), environment={}, default_tag=False
            )
        except RegistryError:
            raise ImageSourcePolicyError("image admission requires a canonical registry reference") from None
        fields = ("endpoint", "repository", "tag", "digest")
        if any(getattr(canonical, field) != getattr(reference, field) for field in fields):
            raise ImageSourcePolicyError("image admission reference contradicts its canonical identity")
        for rule in self.rules:
            if reference.endpoint == rule.registry and reference.repository == rule.full_repository:
                return ImageSourceAdmission(reference, self.digest, rule.namespace)
        raise ImageSourcePolicyError("upstream image source is not allowed by image_sources policy")

    def buildkit_policy(self) -> dict[str, Any]:
        """Restrict image resolution without changing unrelated ADD/Git policy."""
        rules: list[dict[str, Any]] = [{"action": "DENY", "selector": {"identifier": "docker-image://*"}}]
        suffix = r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[a-f0-9]{64})?$"
        for rule in self.rules:
            prefix = re.escape(f"docker-image://{rule.registry}/{rule.full_repository}")
            rules.append({"action": "ALLOW", "selector": {"identifier": "^" + prefix + suffix, "match_type": "REGEX"}})
        return {"version": 1, "rules": rules}


def image_source_policy_from_dict(value: Any) -> ImageSourcePolicy:
    if not isinstance(value, dict) or set(value) != {"schema_version", "publisher_policy", "rules"}:
        raise ImageSourcePolicyError("invalid image_sources fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != _POLICY_SCHEMA:
        raise ImageSourcePolicyError("unsupported image_sources schema_version")
    entries = value["rules"]
    if not isinstance(entries, list) or len(entries) > _MAX_RULES:
        raise ImageSourcePolicyError("image_sources rules exceed the supported bound")
    rules = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"registry", "namespace", "repository"}:
            raise ImageSourcePolicyError("image source rule requires registry, namespace and repository")
        rules.append(ImageSourceRule(**entry))
    return ImageSourcePolicy(tuple(rules), value["publisher_policy"])


def load_image_source_policy(roots: StatePaths) -> ImageSourcePolicy:
    """Read one owner-only main-config snapshot; absent policy denies all images."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        with pinned_owner_directory(roots.config, missing_ok=True) as directory_fd:
            if directory_fd is None:
                return ImageSourcePolicy()
            try:
                descriptor = os.open("config.toml", flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return ImageSourcePolicy()
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) & 0o077
                    or before.st_size > _MAX_CONFIG_BYTES
                ):
                    raise ImageSourcePolicyError("image source config must be an owned owner-only bounded regular file")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(_MAX_CONFIG_BYTES + 1)
                after = os.fstat(descriptor)
                visible = os.stat("config.toml", dir_fd=directory_fd, follow_symlinks=False)

                def identity(item):
                    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)

                if (
                    len(raw) > _MAX_CONFIG_BYTES
                    or identity(before) != identity(after)
                    or identity(after) != identity(visible)
                ):
                    raise ImageSourcePolicyError("image source config changed while reading")
            finally:
                os.close(descriptor)
    except (OSError, StateError):
        raise ImageSourcePolicyError("image source config unavailable or changed while reading") from None
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ImageSourcePolicyError("invalid image source config TOML") from None
    selected = document.get("image_sources")
    return ImageSourcePolicy() if selected is None else image_source_policy_from_dict(selected)


def admit_image_source(
    reference: str,
    roots: StatePaths,
    *,
    config: RegistryConfig | None = None,
    registry_alias: str | None = None,
    dockerfile_source: bool = False,
) -> ImageSourceAdmission:
    """Apply source admission to the existing canonical reference resolution."""
    if dockerfile_source:
        if config is not None or registry_alias is not None:
            raise ImageSourcePolicyError("Dockerfile source identity cannot use a package registry override")
        config = default_registry_config()
        environment: dict[str, str] | None = {}
    else:
        config = config if config is not None else load_registry_config(roots)
        environment = None
    resolved = resolve_image_reference(reference, config, registry_alias=registry_alias, environment=environment)
    profile = config.registries.get(resolved.registry_alias) if resolved.registry_alias is not None else None
    if profile is not None and (profile.protocol != "oci" or profile.plain_http or profile.tls_skip_verify):
        raise ImageSourcePolicyError("upstream image admission requires verified OCI HTTPS")
    return load_image_source_policy(roots).admit(resolved)
