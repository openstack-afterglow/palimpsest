"""Qualify original OCI source bytes against a reviewed native-root OS profile.

The result is descriptive source evidence, never permission to launch a VM. In
particular, this module does not satisfy the native-root execution preflight.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, replace

from .errors import ArtifactValidationError
from .oci_os_release import DEFAULT_OS_READ_LIMITS, ImageOSRelease, OSReadLimits, read_image_os_release
from .oci_provenance import canonical_json_bytes
from .oci_source import LocalArchiveSource, LocalLayoutSource, SnapshottedOCIArchive, SourceCAS
from .platforms import NativeRootBootProfile, select_native_root_boot_profile


@dataclass(frozen=True, slots=True)
class NativeRootSourceQualification:
    """Pinned acquisition and actual OS-file evidence, not an execution receipt."""

    snapshot: SnapshottedOCIArchive
    os_release: ImageOSRelease
    profile: NativeRootBootProfile

    def to_dict(self) -> dict[str, object]:
        image = self.snapshot.image
        return {
            "schema": "palimpsest.native-root-source-qualification.v1",
            "scope": "source-os-profile-only",
            "execution_admitted": False,
            "source_binding_digest": self.snapshot.binding_digest,
            "source": self.snapshot.to_dict(),
            "platform": f"{image.platform.os}/{image.platform.architecture}",
            "index_descriptor": image.index_descriptor.to_dict() if image.index_descriptor is not None else None,
            "layer_occurrences": [occurrence.to_dict() for occurrence in image.layers],
            "os_release": self.os_release.to_dict(),
            "boot_profile": self.profile.to_dict(),
            "reserved_paths_absent": ["/.palimpsest"],
        }

    @property
    def binding_digest(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()


def _validate_target(platform: str, backend: str) -> None:
    # Reject an unreviewed target before touching any source or CAS bytes. The
    # actual distribution and version are selected only after reading the image.
    select_native_root_boot_profile(os_id="ubuntu", version_id="24.04", platform=platform, backend=backend)


def qualify_native_root_source(
    source: LocalArchiveSource | LocalLayoutSource,
    cas: SourceCAS,
    *,
    platform: str,
    backend: str,
    timeout_seconds: float = 300.0,
    maximum_bytes: int = 8 * 1024**3,
) -> NativeRootSourceQualification:
    """Securely snapshot an original local OCI graph and qualify its actual OS.

    Both acquisition and effective-filesystem reading share one cooperative
    deadline. The byte ceiling applies separately to acquisition and cumulative
    OS-reader decompression, including selected-layer replay. No image path is
    extracted and no image program is executed.
    """

    _validate_target(platform, backend)
    if not isinstance(source, (LocalArchiveSource, LocalLayoutSource)) or not isinstance(cas, SourceCAS):
        raise ArtifactValidationError("native-root source qualification requires a local OCI source and SourceCAS")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 3600
    ):
        raise ArtifactValidationError("source qualification timeout must be finite and between 0 and 3600 seconds")
    if type(maximum_bytes) is not int or not 0 < maximum_bytes <= 8 * 1024**3:
        raise ArtifactValidationError("source qualification byte limit must be between 1 and 8589934592")
    deadline = time.monotonic() + timeout_seconds
    snapshot = source.snapshot_acquisition(
        None, cas, platform=platform, timeout_seconds=timeout_seconds, maximum_bytes=maximum_bytes
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArtifactValidationError("native-root source qualification timed out")
    limits = replace(DEFAULT_OS_READ_LIMITS, timeout_seconds=remaining, max_uncompressed_bytes=maximum_bytes)
    result = _qualify_snapshot(snapshot, cas, platform=platform, backend=backend, limits=limits)
    if time.monotonic() >= deadline:
        raise ArtifactValidationError("native-root source qualification timed out")
    return result


def _qualify_snapshot(
    snapshot: SnapshottedOCIArchive,
    cas: SourceCAS,
    *,
    platform: str,
    backend: str,
    limits: OSReadLimits,
) -> NativeRootSourceQualification:
    if not isinstance(snapshot, SnapshottedOCIArchive):
        raise ArtifactValidationError("source qualification requires an acquisition snapshot")
    image = snapshot.image
    if f"{image.platform.os}/{image.platform.architecture}" != platform:
        raise ArtifactValidationError("source qualification platform does not match the pinned OCI graph")
    os_release = read_image_os_release(snapshot, cas, limits=limits)
    if os_release.source_binding_digest != snapshot.binding_digest:
        raise ArtifactValidationError("OS identity does not match the pinned OCI source binding")
    profile = select_native_root_boot_profile(
        os_id=os_release.os_id, version_id=os_release.version_id, platform=platform, backend=backend
    )
    return NativeRootSourceQualification(snapshot, os_release, profile)


def revalidate_native_root_source(
    qualification: NativeRootSourceQualification,
    cas: SourceCAS,
    *,
    limits: OSReadLimits = DEFAULT_OS_READ_LIMITS,
) -> NativeRootSourceQualification:
    """Recompute from pinned CAS bytes; descriptive JSON is never importable authority.

    Revalidation does not confer execution permission. A future execution owner
    must bind source, profile, host capabilities and prepared boot inputs itself.
    """

    if not isinstance(qualification, NativeRootSourceQualification):
        raise ArtifactValidationError("source revalidation requires a pinned qualification object, not a receipt")
    profile = qualification.profile
    _validate_target(profile.platform, profile.backend)
    actual = _qualify_snapshot(
        qualification.snapshot, cas, platform=profile.platform, backend=profile.backend, limits=limits
    )
    if actual != qualification:
        raise ArtifactValidationError("native-root source qualification no longer matches verified source bytes")
    return actual
