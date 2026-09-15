"""Typed operational failures exposed by :mod:`palimpsest_local`."""

from __future__ import annotations


class PalimpsestError(Exception):
    """Base class for expected operational and verification failures."""


class StableFailureError(PalimpsestError):
    """Expected failure carrying only fixed persistence-safe source and category codes."""

    def __init__(self, message: str, *, failure_source: str, failure_category: str) -> None:
        codes = (failure_source, failure_category)
        if any(
            type(code) is not str
            or not code
            or not code.isascii()
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in code)
            for code in codes
        ):
            raise TypeError("stable failure source and category must be lowercase ASCII codes")
        self.failure_source = failure_source
        self.failure_category = failure_category
        super().__init__(message)


class InvalidDigestError(PalimpsestError):
    """A digest is absent or is not canonical SHA-256 syntax."""


class DigestMismatchError(PalimpsestError):
    """Artifact bytes differ from their declared digest."""


class ArtifactValidationError(PalimpsestError):
    """A local image, layer, bundle, or metadata graph is unsafe or invalid."""


class HubError(PalimpsestError):
    """The Hub response violates the supported transfer contract."""


class StateError(PalimpsestError):
    """Local owner-only state cannot safely support an operation."""


class LifecycleError(PalimpsestError):
    """A KVM lifecycle or guest-control operation failed."""


class BuildError(PalimpsestError):
    """A Palimpsestfile or build capture violates the build contract."""


class UnsupportedPlatformError(PalimpsestError):
    """The host environment or system does not support the requested operation."""
