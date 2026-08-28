"""Public pure-contract API for Palimpsest Local."""

from __future__ import annotations

from .digest import digest_file, normalize_digest, require_digest
from .errors import (
    ArtifactValidationError,
    BuildError,
    DigestMismatchError,
    HubError,
    InvalidDigestError,
    LifecycleError,
    PalimpsestError,
    StateError,
    UnsupportedPlatformError,
)
from .hub import HubClient
from .refs import BuildSpec, ImageRef, LayerRef, RunSpec, StackRef

__version__ = "0.1.0.dev0"

__all__ = [
    "ArtifactValidationError",
    "BuildError",
    "BuildSpec",
    "DigestMismatchError",
    "HubClient",
    "HubError",
    "ImageRef",
    "InvalidDigestError",
    "LayerRef",
    "LifecycleError",
    "PalimpsestError",
    "RunSpec",
    "StackRef",
    "StateError",
    "UnsupportedPlatformError",
    "digest_file",
    "normalize_digest",
    "require_digest",
    "__version__",
]
