"""Typed native package references, separate from identifier-only runtime tags.

Records live at state/package-references/<sha256(reference)>.json. A reference
pins both the original selected OCI root and its local archive transport bytes;
a later push must re-snapshot and match both identities before publication.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .digest import require_digest
from .errors import ArtifactValidationError
from .registry import RegistryProfile
from .state import StatePaths, atomic_write_json

_SCHEMA = "palimpsest-local-package-reference-v1"
_MAX_RECORD_BYTES = 64 * 1024


@dataclass(frozen=True)
class LocalPackageReference:
    reference: str
    authority: str
    api_base: str
    namespace: str
    package: str
    archive: str
    archive_digest: str
    archive_size_bytes: int
    root_digest: str
    package_type: str
    project_id: str | None = None
    build_id: str | None = None
    published_digest: str | None = None

    def __post_init__(self) -> None:
        text_fields = (
            self.reference,
            self.authority,
            self.api_base,
            self.namespace,
            self.package,
            self.archive,
            self.archive_digest,
            self.root_digest,
            self.package_type,
        )
        if any(not isinstance(value, str) or not value or len(value) > 4096 for value in text_fields):
            raise ArtifactValidationError("package reference text fields are invalid")
        profile = RegistryProfile(
            alias="reference",
            endpoint=self.authority,
            protocol="palimpsest",
            api_base=self.api_base,
            namespace=self.namespace,
        )
        if profile.endpoint != self.authority or profile.api_base != self.api_base:
            raise ArtifactValidationError("package reference authority/API base is not canonical")
        if not self.namespace or not self.package or len(self.namespace + "/" + self.package) > 255:
            raise ArtifactValidationError("package reference namespace/package is invalid")
        # Reuse the registry repository grammar, including nested package names.
        RegistryProfile(alias="package", endpoint=self.authority, namespace=self.namespace + "/" + self.package)
        prefix = self.authority + "/" + self.namespace + "/" + self.package
        tag_reference = re.fullmatch(re.escape(prefix) + r":[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", self.reference)
        if tag_reference is None and self.reference != prefix + "@" + self.root_digest:
            raise ArtifactValidationError("package reference does not match its target")
        for value in (self.archive_digest, self.root_digest):
            if require_digest(value) != value:
                raise ArtifactValidationError("package reference digest is not canonical")
        if self.published_digest is not None and self.published_digest != self.root_digest:
            raise ArtifactValidationError("publication receipt does not match the local root")
        if self.package_type not in {"oci-image", "runtime-bundle"}:
            raise ArtifactValidationError("package reference has an invalid artifact type")
        if type(self.archive_size_bytes) is not int or self.archive_size_bytes <= 0:
            raise ArtifactValidationError("package reference archive size is invalid")
        if not isinstance(self.archive, str) or not Path(self.archive).is_absolute():
            raise ArtifactValidationError("package reference archive must be an absolute local path")
        if self.project_id is not None and (
            not isinstance(self.project_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.project_id, re.ASCII) is None
        ):
            raise ArtifactValidationError("package reference project identity is invalid")
        if self.build_id is not None and (not isinstance(self.build_id, str) or len(self.build_id) > 128):
            raise ArtifactValidationError("package reference build ID is invalid")


def package_reference_path(roots: StatePaths, reference: str) -> Path:
    identity = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return roots.state / "package-references" / (identity + ".json")


def write_package_reference(roots: StatePaths, record: LocalPackageReference) -> Path:
    path = package_reference_path(roots, record.reference)
    atomic_write_json(path, {"schema": _SCHEMA, **asdict(record)})
    return path


def read_package_reference(roots: StatePaths, reference: str) -> LocalPackageReference:
    path = package_reference_path(roots, reference)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ArtifactValidationError("package reference must be an owner-only regular file")
            if info.st_size > _MAX_RECORD_BYTES:
                raise ArtifactValidationError("package reference exceeds its size bound")
            payload = stream.read(_MAX_RECORD_BYTES + 1)
        data = json.loads(payload)
        if not isinstance(data, dict) or data.pop("schema", None) != _SCHEMA:
            raise ArtifactValidationError("package reference schema is invalid")
        record = LocalPackageReference(**data)
        if record.reference != reference:
            raise ArtifactValidationError("package reference ledger identity mismatch")
        return record
    except FileNotFoundError:
        raise ArtifactValidationError("no native local package reference; build first or supply --input") from None
    except (OSError, TypeError, ValueError):
        raise ArtifactValidationError("invalid native local package reference") from None
