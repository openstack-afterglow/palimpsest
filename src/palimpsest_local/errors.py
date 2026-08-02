"""Typed operational failures exposed by :mod:`palimpsest_local`."""

from __future__ import annotations


class PalimpsestError(Exception):
    """Base class for expected operational and verification failures."""


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
