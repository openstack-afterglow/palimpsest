"""Read-only artifact platform checks; never probe or allocate a runtime.

Architecture compatibility is not proof of GPU/driver, guest ABI, or boot support.
Legacy layers without a declared target remain unknown, even with a known base.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import platforms
from .buildkit import image_arch_for_platform
from .errors import ArtifactValidationError, PalimpsestError

ARCHITECTURES = ("x86_64", "aarch64")


def canonical_arch(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = platforms.normalize_machine(value)
    return normalized if normalized in ARCHITECTURES else None


def _platform_arch(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return image_arch_for_platform(value)
    except ArtifactValidationError:
        return None


def artifact_arch(metadata: Mapping[str, Any]) -> str | None:
    """Read a declared target, including OCI spellings, without host inference."""
    config = metadata.get("config_json")
    config = config if isinstance(config, Mapping) else {}
    value = metadata.get("arch") or config.get("arch")
    platform = metadata.get("platform")
    return canonical_arch(value) or _platform_arch(platform)


def check_artifact(
    metadata: Mapping[str, Any],
    *,
    lookup: Callable[[str], Mapping[str, Any]] | None = None,
    host: platforms.HostPlatform | None = None,
    requested_backend: str = "auto",
) -> dict[str, Any]:
    """Check the declared target and the full cloud-image dependency chain.

    Missing/invisible dependencies are unknown, not compatible. This is metadata
    evaluation only: full runtime preflight still runs when a VM is requested.
    """
    host = host or platforms.detect_host()
    unknown: list[str] = []
    incompatible: list[str] = []
    dependencies: list[dict[str, Any]] = []
    architectures: set[str] = set()
    seen: set[str] = set()

    def descriptor(value: Mapping[str, Any]) -> dict[str, Any]:
        config = value.get("config_json")
        return {**(config if isinstance(config, Mapping) else {}), **{k: v for k, v in value.items() if v is not None}}

    def inspect(value: Mapping[str, Any]) -> dict[str, Any]:
        row = descriptor(value)
        config = value.get("config_json")
        if (
            isinstance(config, Mapping)
            and value.get("arch") is not None
            and config.get("arch") is not None
            and canonical_arch(value["arch"]) != canonical_arch(config["arch"])
        ):
            incompatible.append("artifact and config architectures conflict")
        if row.get("kind") == "cloud-image" and row.get("disk_format") not in {"qcow2", "raw"}:
            incompatible.append("unsupported cloud-image disk format")
        if row.get("availability") in {"missing", "unavailable"}:
            unknown.append("artifact bytes are not locally available")
        arch = artifact_arch(row)
        raw_arch = row.get("arch")
        platform = row.get("platform")
        if raw_arch is not None and canonical_arch(raw_arch) is None:
            incompatible.append("unsupported architecture declaration")
        if platform is not None and _platform_arch(platform) is None:
            incompatible.append("unsupported target platform")
        elif platform is not None and raw_arch is not None and canonical_arch(raw_arch) != _platform_arch(platform):
            incompatible.append("architecture and platform declarations conflict")
        if arch is None:
            unknown.append("artifact architecture is unknown")
        else:
            architectures.add(arch)
        dependencies.append(
            {"digest": row.get("digest") or row.get("blob_digest"), "kind": row.get("kind"), "arch": arch}
        )
        return row

    def fetch(digest: str) -> dict[str, Any] | None:
        if (
            not isinstance(digest, str)
            or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in digest[7:])
        ):
            incompatible.append("invalid dependency digest")
            return None
        if digest in seen:
            incompatible.append("artifact dependency chain is cyclic")
            return None
        seen.add(digest)
        if lookup is None:
            unknown.append("dependency metadata was not checked")
            return None
        try:
            value = lookup(digest)
        except (PalimpsestError, OSError, KeyError, ValueError):
            unknown.append("dependency metadata is missing or unavailable")
            return None
        if not isinstance(value, Mapping):
            unknown.append("dependency metadata is missing or unavailable")
            return None
        actual = value.get("digest") or value.get("blob_digest")
        if actual is not None and actual != digest:
            incompatible.append("dependency metadata digest does not match its reference")
            return None
        return inspect(value)

    row = inspect(metadata)
    kind = row.get("kind")
    initial_digest = row.get("digest") or row.get("blob_digest")
    if isinstance(initial_digest, str):
        seen.add(initial_digest)
    if kind == "squashfs":
        bases: set[str] = set()
        layers = 1
        current = row
        while True:
            base = current.get("base_image_digest")
            if base is not None:
                if not isinstance(base, str):
                    incompatible.append("invalid base-image dependency")
                else:
                    bases.add(base)
            parent = current.get("parent_digest")
            if parent is None:
                if base is None and bases:
                    unknown.append("root layer has no cloud-image base binding")
                break
            if layers >= 25:
                incompatible.append("artifact stack exceeds 25 layers")
                break
            current = fetch(parent)
            if current is None:
                break
            if current.get("kind") != "squashfs":
                incompatible.append("layer parent is not a SquashFS layer")
                break
            layers += 1
        if len(bases) > 1:
            incompatible.append("layer chain declares conflicting base images")
        elif not bases:
            unknown.append("layer chain has no cloud-image base dependency")
        else:
            base_row = fetch(next(iter(bases)))
            if base_row is not None and base_row.get("kind") != "cloud-image":
                incompatible.append("base dependency is not a cloud image")
    elif kind not in {"cloud-image", "oci-image"}:
        unknown.append("artifact has no supported execution contract")

    backend = None
    if len(architectures) > 1:
        incompatible.append("layer and base architectures conflict")
    arch = artifact_arch(row)
    if arch is not None:
        if kind == "oci-image":
            if row.get("platform") != "linux/amd64":
                incompatible.append("OCI-root supports only Linux amd64 images")
            if host.system != "Linux" or host.machine != "x86_64" or requested_backend not in {"auto", "kvm"}:
                incompatible.append("OCI-root requires a Linux x86_64 KVM client")
            else:
                backend = "kvm"
        else:
            if arch != host.machine:
                incompatible.append(f"artifact architecture {arch} differs from client {host.machine}")
            else:
                try:
                    backend = platforms.select_backend(arch, host=host, requested=requested_backend)
                except PalimpsestError as exc:
                    incompatible.append(str(exc))
    status = "incompatible" if incompatible else "unknown" if unknown else "compatible"
    return {
        "status": status,
        "compatible": status == "compatible",
        "arch": arch,
        "platform": {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}.get(arch),
        "client": {"system": host.system, "arch": host.machine, "backend": backend},
        "reasons": list(dict.fromkeys(incompatible + unknown)),
        "dependencies": dependencies,
        "scope": "platform-metadata",
        "runtime_preflight_required": True,
    }
