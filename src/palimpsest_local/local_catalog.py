"""Owner-bound SQLite discovery metadata; never byte, execution or lease authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError, PalimpsestError, StateError
from .oci_image import OCIImageRef
from .oci_provenance import (
    SUPPORTED_COMPRESSED_LAYER_MEDIA_TYPES,
    SUPPORTED_IMAGE_CONFIG_MEDIA_TYPES,
    SUPPORTED_IMAGE_INDEX_MEDIA_TYPES,
    SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES,
    canonical_json_bytes,
)
from .oci_store import DerivedLayerReceipt, OCIStoreError
from .package_reference import LocalPackageReference
from .state import StatePaths

_APPLICATION_ID = 0x504C4331
_SCHEMA_VERSION = 2
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_FILE_FLAGS = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
_SCHEMA = (
    "CREATE TABLE catalog (singleton INTEGER PRIMARY KEY CHECK(singleton=1), catalog_id TEXT NOT NULL, root_binding TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0), captured_at TEXT, warnings TEXT NOT NULL)",
    "CREATE TABLE images (image_id TEXT PRIMARY KEY, root_media TEXT NOT NULL, root_digest TEXT NOT NULL, root_size INTEGER NOT NULL CHECK(root_size>=0), manifest_media TEXT NOT NULL, manifest_digest TEXT NOT NULL, manifest_size INTEGER NOT NULL CHECK(manifest_size>=0), config_media TEXT NOT NULL, config_digest TEXT NOT NULL, config_size INTEGER NOT NULL CHECK(config_size>=0), os TEXT NOT NULL, architecture TEXT NOT NULL, source_binding_digest TEXT NOT NULL, source_cas_id TEXT, source_kind TEXT NOT NULL CHECK(source_kind='oci-root'), materialization_complete INTEGER NOT NULL CHECK(materialization_complete IN (0,1)), source_binding_verified INTEGER NOT NULL CHECK(source_binding_verified IN (0,1)), reference_registry TEXT NOT NULL, reference_repository TEXT NOT NULL, requested_reference TEXT NOT NULL, CHECK((source_binding_verified=1 AND source_cas_id IS NOT NULL) OR (source_binding_verified=0 AND source_cas_id IS NULL AND materialization_complete=0)))",
    "CREATE TABLE source_layers (image_id TEXT NOT NULL REFERENCES images(image_id) ON DELETE CASCADE, ordinal INTEGER NOT NULL CHECK(ordinal>=0), media_type TEXT NOT NULL, compressed_digest TEXT NOT NULL, compressed_size INTEGER NOT NULL CHECK(compressed_size>=0), diff_id TEXT NOT NULL, PRIMARY KEY(image_id,ordinal))",
    "CREATE TABLE derived_layers (image_id TEXT NOT NULL, ordinal INTEGER NOT NULL, store_id TEXT NOT NULL, occurrence_digest TEXT NOT NULL, record_digest TEXT NOT NULL, key_digest TEXT NOT NULL, source_snapshot_binding_digest TEXT NOT NULL, source_image_digest TEXT NOT NULL, image_digest TEXT NOT NULL, image_size INTEGER NOT NULL CHECK(image_size>0), filesystem TEXT NOT NULL CHECK(filesystem='squashfs'), PRIMARY KEY(image_id,ordinal), FOREIGN KEY(image_id,ordinal) REFERENCES source_layers(image_id,ordinal) ON DELETE CASCADE)",
    "CREATE TABLE artifacts (artifact_id INTEGER PRIMARY KEY, category TEXT NOT NULL CHECK(category IN ('images','layers','unknown')), digest TEXT NOT NULL, kind TEXT NOT NULL, projection TEXT NOT NULL)",
    "CREATE TABLE artifact_links (artifact_id INTEGER NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE, role TEXT NOT NULL CHECK(role IN ('base','parent')), ordinal INTEGER NOT NULL CHECK(ordinal>=0), digest TEXT NOT NULL, PRIMARY KEY(artifact_id,role,ordinal))",
    "CREATE TABLE aliases (artifact_id INTEGER NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE, ordinal INTEGER NOT NULL CHECK(ordinal>=0), name TEXT NOT NULL, digest TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('local-tag','native-package-reference')), projection TEXT NOT NULL, PRIMARY KEY(artifact_id,ordinal))",
    "CREATE TABLE builds (build_id TEXT PRIMARY KEY, base_digest TEXT, output_digest TEXT, manifest_digest TEXT, archive_digest TEXT, runtime_digest TEXT, sort_time TEXT NOT NULL, projection TEXT NOT NULL)",
    "CREATE TABLE build_provenance (build_id TEXT PRIMARY KEY REFERENCES builds(build_id) ON DELETE CASCADE, recipe_sha256 TEXT, build_key TEXT, context_digest TEXT, dockerfile_digest TEXT, source_record_digest TEXT, source_record_digest_kind TEXT, CHECK((source_record_digest IS NULL AND source_record_digest_kind IS NULL) OR (source_record_digest IS NOT NULL AND source_record_digest_kind IS NOT NULL AND source_record_digest_kind='canonical-json-semantic-v1')))",
    "CREATE TABLE build_parents (build_id TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE, ordinal INTEGER NOT NULL CHECK(ordinal>=0), digest TEXT NOT NULL, PRIMARY KEY(build_id,ordinal))",
    "CREATE TABLE build_labels (build_id TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE, ordinal INTEGER NOT NULL CHECK(ordinal>=0), label TEXT NOT NULL, PRIMARY KEY(build_id,ordinal))",
    "CREATE TABLE build_artifacts (build_id TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE, artifact_id INTEGER NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE, role TEXT NOT NULL, PRIMARY KEY(build_id,artifact_id,role))",
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, name TEXT NOT NULL, backend TEXT NOT NULL, runtime_kind TEXT NOT NULL, projection TEXT NOT NULL)",
    "CREATE TABLE run_links (run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, role TEXT NOT NULL CHECK(role IN ('base','layer')), ordinal INTEGER NOT NULL CHECK(ordinal>=0), digest TEXT NOT NULL, PRIMARY KEY(run_id,role,ordinal))",
    "CREATE TABLE run_images (run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE, image_id TEXT NOT NULL, source_binding_digest TEXT NOT NULL)",
    "CREATE TABLE observation (singleton INTEGER PRIMARY KEY CHECK(singleton=1), client TEXT NOT NULL)",
)
_SCHEMA_V1 = _SCHEMA
# Version 2 adds Hub publication status and Hub-published pulls. These rows are
# discovery facts about Hub results; they never carry credentials or grant authority.
_SCHEMA_V2_ADDITIONS = (
    "CREATE TABLE publications (request_id TEXT PRIMARY KEY, image_id TEXT NOT NULL REFERENCES images(image_id) ON DELETE RESTRICT, hub_url TEXT NOT NULL, name TEXT NOT NULL, visibility TEXT NOT NULL CHECK(visibility IN ('private','internal','public')), manifest_digest TEXT NOT NULL, source_mode TEXT NOT NULL CHECK(source_mode IN ('closure','none')), source_closure TEXT, generation INTEGER NOT NULL CHECK(generation>=0), terminal INTEGER NOT NULL CHECK(terminal IN (0,1)), hub_image_id TEXT, hub_project_id TEXT, hub_created_by TEXT, hub_request_id TEXT, hub_credential TEXT, status TEXT NOT NULL CHECK(status IN ('pending','published','failed','unknown')), error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(image_id,hub_url,name,visibility,manifest_digest,source_mode,generation), CHECK(status<>'published' OR (hub_image_id IS NOT NULL AND hub_project_id IS NOT NULL)), CHECK((source_mode='none')=(source_closure IS NULL)))",
    "CREATE TABLE pulled_images (hub_url TEXT NOT NULL, hub_image_id TEXT NOT NULL, name TEXT NOT NULL, visibility TEXT NOT NULL CHECK(visibility IN ('private','internal','public')), manifest_digest TEXT NOT NULL, leaf_id TEXT NOT NULL, mode TEXT NOT NULL, platform TEXT NOT NULL, source TEXT NOT NULL CHECK(source='published'), manifest TEXT NOT NULL, source_closure TEXT NOT NULL, source_layout TEXT, local_image_id TEXT, pulled_at TEXT NOT NULL, PRIMARY KEY(hub_url,hub_image_id))",
    "CREATE TABLE pulled_layers (hub_url TEXT NOT NULL, hub_image_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0), node_id TEXT NOT NULL, parent_id TEXT, digest TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes>0), PRIMARY KEY(hub_url,hub_image_id,ordinal), FOREIGN KEY(hub_url,hub_image_id) REFERENCES pulled_images(hub_url,hub_image_id) ON DELETE CASCADE)",
)
_SCHEMA = _SCHEMA_V1 + _SCHEMA_V2_ADDITIONS
_PUBLICATION_FIELDS = (
    "request_id",
    "image_id",
    "hub_url",
    "name",
    "visibility",
    "manifest_digest",
    "source_mode",
    "source_closure",
    "generation",
    "terminal",
    "hub_image_id",
    "hub_project_id",
    "hub_created_by",
    # Request id sent to the Hub when it differs from this row's own id: an own published row's id
    # whose replay could not be confirmed, inherited so the Hub answers the committed image.
    "hub_request_id",
    # Public id (ppi_v1_<hex>, never the secret) of the image key that began this row and last observed
    # its image; NULL for every non-key credential. Only that credential may retry an unpublished row.
    "hub_credential",
    "status",
    "error_code",
    "created_at",
    "updated_at",
)
_VISIBILITIES = ("private", "internal", "public")
_SOURCE_ROLES = ("root", "manifest", "config", "layer")
_PULLED_FIELDS = (
    "hub_url",
    "hub_image_id",
    "name",
    "visibility",
    "manifest_digest",
    "leaf_id",
    "mode",
    "platform",
    "source",
    "manifest",
    "source_closure",
    "source_layout",
    "local_image_id",
    "pulled_at",
)
# Re-pull compatibility compares only facts the client verified itself (or the
# Hub's identity assertions); descriptive projection JSON may legitimately evolve.
_PULLED_COMPARED = (
    "name",
    "visibility",
    "manifest_digest",
    "leaf_id",
    "mode",
    "platform",
    "source_closure",
)
# Fixed vocabulary only: free server/exception text never enters the catalog.
_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}\Z")
_BUILD_FIELDS = frozenset(
    "build_id engine status started_at finished_at duration_ms output_tags output_digest output_oci_manifest_digest output_oci_archive_digest runtime_block_digest base_digest parent_digests platform cache_source timings_ms log_available".split()
)
_BUILD_PROVENANCE_DIGEST_FIELDS = (
    "recipe_sha256",
    "build_key",
    "context_digest",
    "dockerfile_digest",
    "source_record_digest",
)
_BUILD_FIELDS = _BUILD_FIELDS | frozenset(_BUILD_PROVENANCE_DIGEST_FIELDS) | {"source_record_digest_kind"}
_ARTIFACT_FIELDS = frozenset(
    "digest digest_type kind size size_bytes media_type name description arch disk_format virtual_size created_at updated_at tags source availability editable referenced_by delete_supported delete_reason deletion_blockers builds build_ids runtime_block_digest output_tags platform output_oci_manifest_digest output_oci_archive_digest output_oci_archive_size_bytes compatibility parent_digest parent_digests base_digest base_image_digest schema_version filesystem os_release base_root source_kind run_supported materialization_complete image_id upload_source image manifest runtime_kind layers architectures run_limitation output_path oci_root_digest".split()
)
_ARTIFACT_FIELDS = _ARTIFACT_FIELDS | {"package_references"}
_PACKAGE_FIELDS = frozenset(field.name for field in fields(LocalPackageReference))
_RUN_FIELDS = frozenset(
    "id managed source name run_id runtime_kind backend status stale base_digest base_arch layers layer_count memory_mib vcpus network project created_at updated_at lifecycle_revision revision source_image_digest source_binding_digest".split()
)


class CatalogError(StateError):
    """Selected discovery metadata is unavailable or unsafe; never rebuild implicitly."""


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _directory(fd: int, *, private: bool) -> None:
    value = os.fstat(fd)
    mode = stat.S_IMODE(value.st_mode)
    if not stat.S_ISDIR(value.st_mode) or value.st_uid not in (0, os.geteuid()):
        raise CatalogError("catalog directory ownership is unsafe")
    if private:
        if value.st_uid != os.geteuid() or mode != 0o700:
            raise CatalogError("catalog directory must be owner-private")
    elif mode & 0o022 and not (value.st_uid == 0 and mode & stat.S_ISVTX):
        raise CatalogError("catalog ancestor is writable by another owner")


def _canonical_state(path: Path) -> Path:
    """Resolve configured traversal aliases, never the selected final component."""
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise CatalogError("catalog requires an absolute selected state root")
    try:
        return path.parent.resolve() / path.name
    except (OSError, RuntimeError):
        raise CatalogError("catalog state parent cannot be resolved safely") from None


def _open_state(path: Path) -> int | None:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise CatalogError("catalog requires an absolute selected state root")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            _directory(fd, private=False)
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = child
        selected = os.fstat(fd)
        if (
            not stat.S_ISDIR(selected.st_mode)
            or selected.st_uid != os.geteuid()
            or stat.S_IMODE(selected.st_mode) & 0o022
        ):
            raise CatalogError("selected catalog state must be owner-bound and not writable by other owners")
        result, fd = fd, -1
        return result
    except OSError:
        raise CatalogError("selected catalog state cannot be securely opened") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _regular(fd: int) -> None:
    value = os.fstat(fd)
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_nlink != 1
    ):
        raise CatalogError("catalog file must be a single-link owner-private regular file")


def _file(parent: int, name: str, *, create: bool) -> tuple[int, bool]:
    fresh = False
    try:
        if create:
            try:
                fd = os.open(name, _FILE_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
                fresh = True
            except FileExistsError:
                fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
        else:
            fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
        try:
            _regular(fd)
            if _identity(os.fstat(fd)) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                raise CatalogError("catalog file identity changed")
        except BaseException:
            os.close(fd)
            raise
        return fd, fresh
    except OSError:
        raise CatalogError("catalog file cannot be securely opened") from None


class _Files:
    def __init__(self, roots: StatePaths, state_fd: int, directory_fd: int, lock_fd: int):
        self.roots, self.state_fd, self.directory_fd, self.lock_fd = roots, state_fd, directory_fd, lock_fd
        self.db_fd: int | None = None

    def check(self) -> None:
        current = _open_state(self.roots.state)
        try:
            if current is None or _identity(os.fstat(current)) != _identity(os.fstat(self.state_fd)):
                raise CatalogError("selected catalog state identity changed")
        finally:
            if current is not None:
                os.close(current)
        _directory(self.directory_fd, private=True)
        if _identity(os.fstat(self.directory_fd)) != _identity(
            os.stat("catalog-v1", dir_fd=self.state_fd, follow_symlinks=False)
        ):
            raise CatalogError("catalog directory identity changed")
        for name, fd in (("writer.lock", self.lock_fd), ("metadata.sqlite3", self.db_fd)):
            if fd is not None:
                _regular(fd)
                if _identity(os.fstat(fd)) != _identity(os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)):
                    raise CatalogError("catalog file identity changed")
        for name in os.listdir(self.directory_fd):
            if name not in ("writer.lock", "metadata.sqlite3", "metadata.sqlite3-journal"):
                raise CatalogError("catalog contains an unsupported file")
            if name == "metadata.sqlite3-journal":
                fd, _ = _file(self.directory_fd, name, create=False)
                os.close(fd)

    def binding(self) -> str:
        value = os.fstat(self.state_fd)
        return _json([str(self.roots.state), value.st_dev, value.st_ino])


@contextmanager
def _locked(
    roots: StatePaths, *, create: bool, timeout_seconds: float, check_exit: bool = True
) -> Iterator[_Files | None]:
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 300
    ):
        raise CatalogError("catalog timeout must be finite and between zero and 300 seconds")
    if type(create) is not bool:
        raise CatalogError("catalog creation policy is invalid")
    roots = StatePaths(config=roots.config, state=_canonical_state(roots.state))
    state_fd = _open_state(roots.state)
    directory_fd = lock_fd = None
    try:
        if state_fd is None:
            yield None
            return
        try:
            if create:
                try:
                    os.mkdir("catalog-v1", 0o700, dir_fd=state_fd)
                    os.fsync(state_fd)
                except FileExistsError:
                    pass
            directory_fd = os.open("catalog-v1", _DIR_FLAGS, dir_fd=state_fd)
        except FileNotFoundError:
            yield None
            return
        _directory(directory_fd, private=True)
        # A present subtree is never silently repaired by a read-only open.
        lock_fd, _ = _file(directory_fd, "writer.lock", create=create)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CatalogError("catalog writer lock timed out") from None
                time.sleep(min(0.025, remaining))
        files = _Files(roots, state_fd, directory_fd, lock_fd)
        files.check()
        yield files
        if check_exit:
            files.check()
    except OSError:
        raise CatalogError("catalog filesystem operation failed") from None
    finally:
        for fd in (lock_fd, directory_fd, state_fd):
            if fd is not None:
                os.close(fd)


def _json(value: Any) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        encoded_size = len(result.encode("utf-8"))
    except (TypeError, ValueError):
        raise CatalogError("catalog metadata is not finite UTF-8 JSON") from None
    if encoded_size > 1024 * 1024:
        raise CatalogError("catalog metadata exceeds its bounded projection")
    return result


def _projection(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError("catalog projection must be an object")
    result = {key: item for key, item in value.items() if key in fields}

    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    not isinstance(key, str)
                    or any(
                        word in key.lower()
                        for word in ("password", "secret", "token", "private_key", "environment", "argv")
                    )
                    or key.lower() in ("env", "config", "process")
                ):
                    raise CatalogError("protected state is forbidden in catalog projections")
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
        elif isinstance(item, str) and "-----BEGIN" in item and "KEY-----" in item:
            raise CatalogError("key material is forbidden in catalog projections")

    check(result)
    return json.loads(_json(result))


def _digest(value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise CatalogError("catalog digest is not canonical")
    return value


def validate_catalog_text(value: Any) -> str:
    """Validate shared bounded UTF-8 text before metadata transactions."""
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise CatalogError("catalog text identity is invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CatalogError("catalog text identity is not valid UTF-8") from None
    return value


def canonical_hub_url(value: Any) -> str:
    """Return ``scheme://host[:port][/path]``; refuse embedded credentials, query or fragment.

    Error text never repeats the input, which could contain a credential.
    """
    text = validate_catalog_text(value).strip()
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise CatalogError("Hub URL is invalid") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise CatalogError("Hub URL must not embed credentials; supply them through the environment")
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "?" in text or "#" in text:
        raise CatalogError("Hub URL must be an http(s) URL without query or fragment")
    scheme = parts.scheme.lower()
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    if port == {"https": 443, "http": 80}[scheme]:
        port = None
    return f"{scheme}://{host}{'' if port is None else ':' + str(port)}{parts.path.rstrip('/')}"


def validate_hub_url(value: Any) -> str:
    """Accept only an already canonical credential-free Hub URL."""
    text = validate_catalog_text(value)
    if canonical_hub_url(text) != text:
        raise CatalogError("Hub URL must be a canonical credential-free http(s) URL")
    return text


def _uuid_text(value: Any) -> str:
    try:
        if isinstance(value, str) and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise CatalogError("catalog identity is not a canonical UUID")


def _publication_name(value: Any) -> str:
    name = validate_catalog_text(value)
    if len(name) > 256 or not name.strip():
        raise CatalogError("catalog publication name is invalid")
    return name


def _credential_id(value: Any) -> str | None:
    """Public image-key id only (``ppi_v1_<32 hex>``); a secret can never match this shape."""
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"ppi_v1_[0-9a-f]{32}", value) is None:
        raise CatalogError("catalog credential identity must be a public image-key id")
    return value


def _hub_identity(value: Any) -> str:
    """Opaque Hub project/user identity: bounded printable ASCII without spaces."""
    if not isinstance(value, str) or re.fullmatch(r"[\x21-\x7e]{1,255}", value) is None:
        raise CatalogError("catalog Hub identity is invalid")
    return value


def _source_closure(value: Any) -> list[dict]:
    """Canonical OCI source-closure descriptors: one manifest/config, optional root, ordered layers."""
    if not isinstance(value, list) or not 1 <= len(value) <= 300:
        raise CatalogError("catalog source closure is invalid")
    items, sizes = {}, {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "digest", "size_bytes", "ordinal"}:
            raise CatalogError("catalog source closure item is invalid")
        role, size, ordinal = item["role"], item["size_bytes"], item["ordinal"]
        digest = _digest(item["digest"])
        if (
            role not in _SOURCE_ROLES
            or type(size) is not int
            or not 0 < size < 2**63
            or type(ordinal) is not int
            or not 0 <= ordinal < 300
            or (role, ordinal) in items
            or sizes.setdefault(digest, size) != size
        ):
            raise CatalogError("catalog source closure item is invalid")
        items[(role, ordinal)] = {"role": role, "digest": digest, "size_bytes": size, "ordinal": ordinal}
    ordinals = {role: sorted(ordinal for kind, ordinal in items if kind == role) for role in _SOURCE_ROLES}
    if (
        ordinals["manifest"] != [0]
        or ordinals["config"] != [0]
        or ordinals["root"] not in ([], [0])
        or not ordinals["layer"]
        or ordinals["layer"] != list(range(len(ordinals["layer"])))
    ):
        raise CatalogError("catalog source closure roles are incomplete")
    return [items[key] for key in sorted(items, key=lambda key: (_SOURCE_ROLES.index(key[0]), key[1]))]


def _descriptor(value: Any) -> tuple[str, str, int]:
    if not isinstance(value, dict) or set(value) != {"mediaType", "digest", "size"}:
        raise CatalogError("catalog descriptor is invalid")
    media, digest, size = validate_catalog_text(value["mediaType"]), _digest(value["digest"]), value["size"]
    if type(size) is not int or not 0 <= size < 2**63:
        raise CatalogError("catalog descriptor size is invalid")
    return media, digest, size


def validate_catalog_timing(name: Any, value: Any) -> tuple[str, int | float]:
    """Validate one public finite timing field using the projection's key policy."""
    name = validate_catalog_text(name)
    _projection({name: value}, frozenset((name,)))
    valid = type(value) is int or (type(value) is float and math.isfinite(value))
    if not valid:
        raise CatalogError("catalog timing is not a finite number")
    return name, value


def prepare_build_observation(value: Any) -> dict[str, Any]:
    """Admit one safe observational build, without opening SQL or source files."""
    build = _projection(value, _BUILD_FIELDS)
    validate_catalog_text(build.get("build_id"))
    for name in (
        "base_digest",
        "output_digest",
        "output_oci_manifest_digest",
        "output_oci_archive_digest",
        "runtime_block_digest",
        *_BUILD_PROVENANCE_DIGEST_FIELDS,
    ):
        _digest(build.get(name), optional=True)
    for name in ("started_at", "finished_at"):
        if build.get(name) is not None and not isinstance(build[name], str):
            raise CatalogError("catalog build timestamp is invalid")
    parents = build.get("parent_digests", [])
    labels = build.get("output_tags", [])
    timings = build.get("timings_ms", {})
    if not isinstance(parents, list) or not isinstance(labels, list) or not isinstance(timings, dict):
        raise CatalogError("catalog build occurrence or timing shape is invalid")
    for digest in parents:
        _digest(digest)
    for label in labels:
        validate_catalog_text(label)
    for name, timing in timings.items():
        validate_catalog_timing(name, timing)
    source_digest = build.get("source_record_digest")
    source_kind = build.get("source_record_digest_kind")
    if (source_digest is None and source_kind is not None) or (
        source_digest is not None and source_kind != "canonical-json-semantic-v1"
    ):
        raise CatalogError("catalog source build record binding kind is invalid")
    return build


def prepare_artifact_observation(value: Any) -> dict[str, Any]:
    """Admit one artifact projection and its descriptive relationships, never authority."""
    artifact = _projection(value, _ARTIFACT_FIELDS)
    digest = _digest(artifact.get("digest"))
    validate_catalog_text(artifact.get("kind", "unknown"))
    for name in ("base_digest", "base_image_digest", "parent_digest"):
        _digest(artifact.get(name), optional=True)
    parents = artifact.get("parent_digests", [])
    tags = artifact.get("tags", [])
    builds = artifact.get("builds", [])
    references = artifact.get("package_references", [])
    labels = artifact.get("output_tags", [])
    if not all(isinstance(items, list) for items in (parents, tags, builds, references, labels)):
        raise CatalogError("catalog artifact relationship shape is invalid")
    for parent in parents:
        _digest(parent)
    for label in labels:
        validate_catalog_text(label)
    for alias in tags:
        safe = _projection(
            alias, frozenset(("name", "tag", "digest", "kind", "created_at", "updated_at", "schema_version"))
        )
        validate_catalog_text(safe.get("name") or safe.get("tag"))
        if _digest(safe.get("digest")) != digest:
            raise CatalogError("catalog local tag does not match its artifact")
    if "builds" in artifact:
        artifact["builds"] = [prepare_build_observation(build) for build in builds]
    if "upload_source" in artifact:
        artifact["upload_source"] = _projection(
            artifact["upload_source"], frozenset(("kind", "connection_id", "reference"))
        )
    projected_references = []
    for reference in references:
        try:
            record = LocalPackageReference(**_projection(reference, _PACKAGE_FIELDS))
        except (PalimpsestError, TypeError, ValueError):
            raise CatalogError("catalog native package reference is invalid") from None
        if record.root_digest != digest:
            raise CatalogError("catalog native package alias does not match its artifact")
        projected_references.append(asdict(record))
    if "package_references" in artifact:
        artifact["package_references"] = projected_references
    _json(artifact)
    return artifact


def prepare_run_observation(value: Any) -> dict[str, Any]:
    """Admit one UUID-bound ledger projection without granting runtime control."""
    run = _projection(value, _RUN_FIELDS)
    run_id = validate_catalog_text(run.get("run_id"))
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError
    except ValueError:
        raise CatalogError("catalog run UUID is invalid") from None
    for name in ("name", "backend", "runtime_kind"):
        validate_catalog_text(run.get(name))
    _digest(run.get("base_digest"), optional=True)
    layers = run.get("layers", [])
    if not isinstance(layers, list):
        raise CatalogError("catalog run layer occurrence shape is invalid")
    for layer in layers:
        _digest(layer.get("digest") if isinstance(layer, dict) else layer)
    if "source_image_digest" in run or "source_binding_digest" in run:
        _digest(run.get("source_image_digest"))
        _digest(run.get("source_binding_digest"))
    return run


class Catalog:
    """Scoped discovery handle. All callers must use ``open_catalog``."""

    def __init__(self, connection: sqlite3.Connection, files: _Files, *, rebind_from: Path | None):
        self._connection, self._files, self._rebind_from = connection, files, rebind_from
        self._closed = False
        self._version = _SCHEMA_VERSION

    def _check(self) -> None:
        if self._closed:
            raise CatalogError("catalog handle is closed")
        self._files.check()
        binding = self._connection.execute("SELECT root_binding FROM catalog WHERE singleton=1").fetchone()[0]
        if self._rebind_from is None and binding != self._files.binding():
            raise CatalogError("catalog belongs to a different selected state root")
        if self._rebind_from is not None:
            if json.loads(binding)[0] != str(self._rebind_from):
                raise CatalogError("catalog relocation source binding differs")

    @contextmanager
    def _write(self) -> Iterator[None]:
        self._check()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield
            self._files.check()
            self._connection.execute("COMMIT")
            os.fsync(self._files.directory_fd)
        except BaseException as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise CatalogError("catalog transaction failed; previous metadata was retained") from None
            raise

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._connection.in_transaction:
                self._connection.rollback()
            self._connection.close()
            self._files.check()
        finally:
            self._closed = True

    def replace_observation(
        self, *, artifacts: dict, builds: list[dict], runs: list[dict], warnings: list[str], captured_at: str
    ) -> int:
        self._check()
        if (
            not isinstance(artifacts, dict)
            or not isinstance(builds, list)
            or not isinstance(runs, list)
            or not isinstance(warnings, list)
            or any(not isinstance(item, str) for item in warnings)
        ):
            raise CatalogError("catalog observation shape is invalid")
        captured_at = validate_catalog_text(captured_at)
        warning_json = _json(list(dict.fromkeys(warnings)))
        client = _projection(artifacts.get("client", {}), frozenset(("system", "arch")))
        projected_builds = [prepare_build_observation(item) for item in builds]
        projected_runs = [prepare_run_observation(item) for item in runs]
        category_by_digest = {}
        for category in ("images", "layers", "unknown"):
            entries = artifacts.get(category, [])
            if not isinstance(entries, list):
                raise CatalogError("catalog artifact category shape is invalid")
            for artifact in entries:
                if not isinstance(artifact, dict):
                    raise CatalogError("catalog artifact category entry is invalid")
                digest = _digest(artifact.get("digest"))
                kind = validate_catalog_text(artifact.get("kind", "unknown"))
                category_by_digest[(digest, kind)] = category
        entries = artifacts.get("artifacts", [])
        if not isinstance(entries, list):
            raise CatalogError("catalog artifacts must be an ordered list")
        projected_artifacts = [prepare_artifact_observation(item) for item in entries]
        with self._write():
            for table in ("artifacts", "builds", "runs", "observation"):
                self._connection.execute(f"DELETE FROM {table}")
            for artifact in projected_artifacts:
                digest = _digest(artifact.get("digest"))
                category = category_by_digest.get((digest, artifact.get("kind", "unknown")), "unknown")
                row = self._connection.execute(
                    "INSERT INTO artifacts(category,digest,kind,projection) VALUES(?,?,?,?)",
                    (category, digest, validate_catalog_text(artifact.get("kind", "unknown")), _json(artifact)),
                )
                artifact_id = row.lastrowid
                base_digest = artifact.get("base_digest") or artifact.get("base_image_digest")
                for role, values in (
                    ("base", [base_digest] if base_digest else []),
                    (
                        "parent",
                        artifact.get(
                            "parent_digests", [artifact["parent_digest"]] if artifact.get("parent_digest") else []
                        ),
                    ),
                ):
                    for ordinal, value in enumerate(values):
                        self._connection.execute(
                            "INSERT INTO artifact_links VALUES(?,?,?,?)", (artifact_id, role, ordinal, _digest(value))
                        )
                for ordinal, alias in enumerate(artifact.get("tags", [])):
                    safe = _projection(
                        alias,
                        frozenset(("name", "tag", "digest", "kind", "created_at", "updated_at", "schema_version")),
                    )
                    self._connection.execute(
                        "INSERT INTO aliases VALUES(?,?,?,?,?,?)",
                        (
                            artifact_id,
                            ordinal,
                            validate_catalog_text(safe.get("name") or safe.get("tag")),
                            _digest(safe.get("digest")),
                            "local-tag",
                            _json(safe),
                        ),
                    )
                for ordinal, reference in enumerate(
                    artifact.get("package_references", []), start=len(artifact.get("tags", []))
                ):
                    self._connection.execute(
                        "INSERT INTO aliases VALUES(?,?,?,?,?,?)",
                        (
                            artifact_id,
                            ordinal,
                            reference["reference"],
                            reference["root_digest"],
                            "native-package-reference",
                            _json(reference),
                        ),
                    )
            for build in projected_builds:
                build_id = validate_catalog_text(build.get("build_id"))
                digests = [
                    _digest(build.get(field), optional=True)
                    for field in (
                        "base_digest",
                        "output_digest",
                        "output_oci_manifest_digest",
                        "output_oci_archive_digest",
                        "runtime_block_digest",
                    )
                ]
                self._connection.execute(
                    "INSERT INTO builds VALUES(?,?,?,?,?,?,?,?)",
                    (build_id, *digests, build.get("started_at") or build.get("finished_at") or "", _json(build)),
                )
                provenance = [_digest(build.get(field), optional=True) for field in _BUILD_PROVENANCE_DIGEST_FIELDS]
                provenance_kind = build.get("source_record_digest_kind")
                self._connection.execute(
                    "INSERT INTO build_provenance VALUES(?,?,?,?,?,?,?)", (build_id, *provenance, provenance_kind)
                )
                for ordinal, digest in enumerate(build.get("parent_digests", [])):
                    self._connection.execute(
                        "INSERT INTO build_parents VALUES(?,?,?)", (build_id, ordinal, _digest(digest))
                    )
                for ordinal, label in enumerate(build.get("output_tags", [])):
                    self._connection.execute(
                        "INSERT INTO build_labels VALUES(?,?,?)", (build_id, ordinal, validate_catalog_text(label))
                    )
                for role, digest in zip(("base", "output", "manifest", "archive", "runtime"), digests, strict=True):
                    if digest:
                        self._connection.execute(
                            "INSERT INTO build_artifacts SELECT ?,artifact_id,? FROM artifacts WHERE digest=?",
                            (build_id, role, digest),
                        )
            for run in projected_runs:
                run_id = validate_catalog_text(run.get("run_id"))
                self._connection.execute(
                    "INSERT INTO runs VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        validate_catalog_text(run.get("name")),
                        validate_catalog_text(run.get("backend")),
                        validate_catalog_text(run.get("runtime_kind")),
                        _json(run),
                    ),
                )
                if "source_image_digest" in run or "source_binding_digest" in run:
                    self._connection.execute(
                        "INSERT INTO run_images VALUES(?,?,?)",
                        (run_id, _digest(run.get("source_image_digest")), _digest(run.get("source_binding_digest"))),
                    )
                if run.get("base_digest"):
                    self._connection.execute(
                        "INSERT INTO run_links VALUES(?,?,?,?)", (run_id, "base", 0, _digest(run["base_digest"]))
                    )
                for ordinal, layer in enumerate(run.get("layers", [])):
                    self._connection.execute(
                        "INSERT INTO run_links VALUES(?,?,?,?)",
                        (run_id, "layer", ordinal, _digest(layer.get("digest") if isinstance(layer, dict) else layer)),
                    )
            self._connection.execute("INSERT INTO observation VALUES(1,?)", (_json(client),))
            self._connection.execute(
                "UPDATE catalog SET revision=revision+1,captured_at=?,warnings=? WHERE singleton=1",
                (captured_at, warning_json),
            )
            revision = self._connection.execute("SELECT revision FROM catalog").fetchone()[0]
        return revision

    def read_observation(self) -> dict:
        self._check()
        row = self._connection.execute(
            "SELECT catalog_id,revision,captured_at,warnings FROM catalog WHERE singleton=1"
        ).fetchone()
        warnings = json.loads(row[3])
        return {
            "catalog_id": row[0],
            "revision": row[1],
            "captured_at": row[2],
            "complete": row[2] is not None and not warnings,
            "warnings": warnings,
        }

    def read_artifacts(self) -> dict:
        self._check()
        result = {"artifacts": [], "images": [], "layers": [], "unknown": []}
        for category, projection in self._connection.execute(
            "SELECT category,projection FROM artifacts ORDER BY artifact_id"
        ):
            artifact = json.loads(projection)
            result["artifacts"].append(artifact)
            result[category].append(artifact)
        client = self._connection.execute("SELECT client FROM observation WHERE singleton=1").fetchone()
        if client is not None:
            result["client"] = json.loads(client[0])
        result["observation"] = self.read_observation()
        return result

    def read_builds(self, *, limit: int = 100) -> list[dict]:
        self._check()
        if type(limit) is not int or not 0 <= limit < 2**63:
            raise CatalogError("catalog build limit is invalid")
        return [
            json.loads(row[0])
            for row in self._connection.execute(
                "SELECT projection FROM builds ORDER BY sort_time DESC,build_id LIMIT ?", (limit,)
            )
        ]

    def get_build(self, build_id: str) -> dict:
        self._check()
        row = self._connection.execute(
            "SELECT projection FROM builds WHERE build_id=?", (validate_catalog_text(build_id),)
        ).fetchone()
        if row is None:
            raise CatalogError("catalog build was not found")
        return json.loads(row[0])

    def read_runs(self) -> list[dict]:
        self._check()
        return [
            json.loads(row[0])
            for row in self._connection.execute("SELECT projection FROM runs ORDER BY name,backend,run_id")
        ]

    def read_snapshot(self) -> dict:
        return {
            "observation": self.read_observation(),
            "images": self.read_images(),
            "builds": self.read_builds(limit=2**63 - 1),
            "runs": self.read_runs(),
            "artifacts": self.read_artifacts(),
        }

    def register_image(self, image: dict) -> None:
        fields = {
            "image_id",
            "reference",
            "root_descriptor",
            "manifest_descriptor",
            "config_descriptor",
            "platform",
            "layers",
            "source_binding_digest",
            "source_cas_id",
            "source_kind",
            "derived",
            "materialization_complete",
        }
        if not isinstance(image, dict) or set(image) != fields:
            raise CatalogError("catalog source graph shape is invalid")
        reference = image["reference"]
        if not isinstance(reference, dict) or set(reference) != {"registry", "repository", "requested_reference"}:
            raise CatalogError("catalog original reference is invalid")
        try:
            original_reference = OCIImageRef(**reference)
        except (ArtifactValidationError, TypeError, ValueError):
            raise CatalogError("catalog original reference is invalid") from None
        if original_reference.to_dict() != reference:
            raise CatalogError("catalog original reference is not canonical")
        reference_fields = (
            original_reference.registry,
            original_reference.repository,
            original_reference.requested_reference,
        )
        image_id = _digest(image["image_id"])
        descriptors = [
            _descriptor(image[key]) for key in ("root_descriptor", "manifest_descriptor", "config_descriptor")
        ]
        platform = image["platform"]
        if (
            not isinstance(platform, dict)
            or set(platform) != {"os", "architecture"}
            or platform["os"] != "linux"
            or platform["architecture"] not in ("amd64", "arm64")
        ):
            raise CatalogError("catalog source platform is unsupported")
        if (
            not isinstance(image["source_cas_id"], str)
            or re.fullmatch(r"source-cas-v1:[0-9a-f]{64}", image["source_cas_id"]) is None
            or image["source_kind"] != "oci-root"
        ):
            raise CatalogError("catalog source binding is invalid")
        binding = _digest(image["source_binding_digest"])
        layers = image["layers"]
        if (
            not isinstance(layers, list)
            or not 1 <= len(layers) <= 128
            or type(image["materialization_complete"]) is not bool
            or not isinstance(image["derived"], list)
        ):
            raise CatalogError("catalog source occurrence count is invalid")
        source_rows = []
        for ordinal, layer in enumerate(layers):
            if (
                not isinstance(layer, dict)
                or set(layer) != {"ordinal", "compressed", "diff_id"}
                or type(layer["ordinal"]) is not int
                or layer["ordinal"] != ordinal
            ):
                raise CatalogError("catalog source ordinals are not contiguous")
            source_rows.append((image_id, ordinal, *_descriptor(layer["compressed"]), _digest(layer["diff_id"])))
        if (
            descriptors[0][0] not in SUPPORTED_IMAGE_INDEX_MEDIA_TYPES | SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES
            or descriptors[1][0] not in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES
            or descriptors[2][0] not in SUPPORTED_IMAGE_CONFIG_MEDIA_TYPES
            or any(row[2] not in SUPPORTED_COMPRESSED_LAYER_MEDIA_TYPES for row in source_rows)
        ):
            raise CatalogError("catalog descriptor media type is unsupported")
        if descriptors[0][0] in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES and descriptors[0] != descriptors[1]:
            raise CatalogError("catalog root manifest descriptor is inconsistent")
        members = {
            "cas_id": image["source_cas_id"],
            "config": image["config_descriptor"],
            "domain": "palimpsest.source-snapshot.v1",
            "image_digest": image_id,
            "layers": [layer["compressed"] for layer in layers],
            "manifest": image["manifest_descriptor"],
            "root": image["root_descriptor"],
        }
        if binding != "sha256:" + hashlib.sha256(canonical_json_bytes(members)).hexdigest():
            raise CatalogError("catalog source snapshot binding is inconsistent")
        receipts = []
        for ordinal, value in enumerate(image["derived"]):
            try:
                receipt = DerivedLayerReceipt.from_dict(value)
            except (OCIStoreError, TypeError, ValueError):
                raise CatalogError("catalog derived occurrence is invalid") from None
            if (
                receipt.ordinal != ordinal
                or receipt.source_image_digest != image_id
                or receipt.source_snapshot_binding_digest != binding
                or ordinal >= len(layers)
            ):
                raise CatalogError("catalog derived occurrence does not match source graph")
            receipts.append(receipt)
        if (
            len({receipt.store_id for receipt in receipts}) > 1
            or image["materialization_complete"]
            and len(receipts) != len(layers)
        ):
            raise CatalogError("catalog complete materialization lacks an exact occurrence sequence")
        graph = (
            image_id,
            *(part for descriptor in descriptors for part in descriptor),
            platform["os"],
            platform["architecture"],
            binding,
            image["source_cas_id"],
            "oci-root",
        )
        with self._write():
            existing = self._connection.execute("SELECT * FROM images WHERE image_id=?", (image_id,)).fetchone()
            if existing is not None:
                old_layers = self._connection.execute(
                    "SELECT * FROM source_layers WHERE image_id=? ORDER BY ordinal", (image_id,)
                ).fetchall()
                if (
                    tuple(existing[:12]) != graph[:12]
                    or tuple(existing[17:20]) != reference_fields
                    or existing[14] != graph[14]
                    or old_layers != source_rows
                    or existing[16]
                    and tuple(existing[:15]) != graph
                ):
                    raise CatalogError("catalog registered source graph is immutable")
                old_receipts = self._derived(image_id)
                if receipts and old_receipts[: min(len(receipts), len(old_receipts))] != [
                    receipt.to_dict() for receipt in receipts[: len(old_receipts)]
                ]:
                    raise CatalogError("catalog registered derived occurrence is immutable")
                # Re-registering source-only facts never demotes a completed receipt.
                if len(receipts) < len(old_receipts):
                    return
            else:
                self._connection.execute(
                    "INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*graph, int(image["materialization_complete"]), 1, *reference_fields),
                )
                self._connection.executemany("INSERT INTO source_layers VALUES(?,?,?,?,?,?)", source_rows)
            self._connection.execute(
                "UPDATE images SET source_binding_digest=?,source_cas_id=?,source_binding_verified=1 WHERE image_id=?",
                (binding, image["source_cas_id"], image_id),
            )
            self._connection.execute("DELETE FROM derived_layers WHERE image_id=?", (image_id,))
            for receipt in receipts:
                self._connection.execute(
                    "INSERT INTO derived_layers VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        image_id,
                        receipt.ordinal,
                        receipt.store_id,
                        receipt.occurrence_digest,
                        receipt.record_digest,
                        receipt.key_digest,
                        receipt.source_snapshot_binding_digest,
                        receipt.source_image_digest,
                        receipt.image_digest,
                        receipt.image_size,
                        receipt.filesystem,
                    ),
                )
            self._connection.execute(
                "UPDATE images SET materialization_complete=MAX(materialization_complete,?) WHERE image_id=?",
                (int(image["materialization_complete"]), image_id),
            )

    def _derived(self, image_id: str) -> list[dict]:
        names = (
            "ordinal",
            "store_id",
            "occurrence_digest",
            "record_digest",
            "key_digest",
            "source_snapshot_binding_digest",
            "source_image_digest",
            "image_digest",
            "image_size",
            "filesystem",
        )
        return [
            dict(zip(names, row, strict=True))
            for row in self._connection.execute(
                "SELECT ordinal,store_id,occurrence_digest,record_digest,key_digest,source_snapshot_binding_digest,source_image_digest,image_digest,image_size,filesystem FROM derived_layers WHERE image_id=? ORDER BY ordinal",
                (image_id,),
            )
        ]

    def read_images(self) -> list[dict]:
        self._check()
        result = []
        for row in self._connection.execute("SELECT * FROM images ORDER BY image_id"):
            image = {"image_id": row[0]}
            for offset, name in ((1, "root_descriptor"), (4, "manifest_descriptor"), (7, "config_descriptor")):
                image[name] = dict(zip(("mediaType", "digest", "size"), row[offset : offset + 3], strict=True))
            image.update(
                platform={"os": row[10], "architecture": row[11]},
                source_binding_digest=row[12],
                source_cas_id=row[13],
                source_kind=row[14],
                materialization_complete=bool(row[15]),
                source_binding_verified=bool(row[16]),
            )
            image["reference"] = dict(zip(("registry", "repository", "requested_reference"), row[17:20], strict=True))
            image["layers"] = [
                {
                    "ordinal": ordinal,
                    "compressed": {"mediaType": media, "digest": digest, "size": size},
                    "diff_id": diff_id,
                }
                for ordinal, media, digest, size, diff_id in self._connection.execute(
                    "SELECT ordinal,media_type,compressed_digest,compressed_size,diff_id FROM source_layers WHERE image_id=? ORDER BY ordinal",
                    (row[0],),
                )
            ]
            image["derived"] = self._derived(row[0])
            image["materialization_status"] = (
                "source-unavailable"
                if not row[16]
                else "complete"
                if row[15]
                else "partial"
                if image["derived"]
                else "source-only"
            )
            result.append(image)
        return result

    def _upgrade(self) -> bool:
        """Add version-2 tables inside the caller's write transaction only."""
        if self._version == _SCHEMA_VERSION:
            return False
        for statement in _SCHEMA_V2_ADDITIONS:
            self._connection.execute(statement)
        self._connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        return True

    def _publication(self, request_id: str) -> dict | None:
        row = self._connection.execute(
            f"SELECT {','.join(_PUBLICATION_FIELDS)} FROM publications WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(zip(_PUBLICATION_FIELDS, row, strict=True))
        record["terminal"] = bool(record["terminal"])
        if record["source_closure"] is not None:
            record["source_closure"] = json.loads(record["source_closure"])
        return record

    def _intent(
        self, image_id: str, hub_url: str, name: str, visibility: str, manifest_digest: str, source_closure: Any
    ) -> tuple[tuple, str | None]:
        image_id, manifest_digest = _digest(image_id), _digest(manifest_digest)
        hub_url, name = validate_hub_url(hub_url), _publication_name(name)
        if visibility not in _VISIBILITIES:
            raise CatalogError("catalog publication visibility is invalid")
        closure = None if source_closure is None else _json(_source_closure(source_closure))
        return (image_id, hub_url, name, visibility, manifest_digest, "none" if closure is None else "closure"), closure

    def intent_publications(
        self,
        *,
        image_id: str,
        hub_url: str,
        name: str,
        visibility: str,
        manifest_digest: str,
        source_closure: list[dict] | None,
        credential: str | None = None,
    ) -> list[dict]:
        """Generations of one publication intent visible to ``credential``, newest first (read-only).

        ``published`` rows are visible to every credential (each revalidates them against the Hub). An
        unpublished row is an attempt owned by the credential that began it, identified by its public image-key
        id (``None`` for every non-key credential): no other credential may retry, adopt or end it.
        """
        key, _ = self._intent(image_id, hub_url, name, visibility, manifest_digest, source_closure)
        credential = _credential_id(credential)
        self._check()
        if self._version != _SCHEMA_VERSION:
            return []
        return [
            self._publication(row[0])
            for row in self._connection.execute(
                "SELECT request_id FROM publications WHERE image_id=? AND hub_url=? AND name=? AND visibility=? AND manifest_digest=? AND source_mode=? AND (status='published' OR hub_credential IS ?) ORDER BY generation DESC",
                (*key, credential),
            ).fetchall()
        ]

    def begin_publication(
        self,
        *,
        image_id: str,
        hub_url: str,
        name: str,
        visibility: str,
        manifest_digest: str,
        source_closure: list[dict] | None,
        now: str,
        stale_request_id: str | None = None,
        credential: str | None = None,
        inherit_hub_request_id: str | None = None,
    ) -> dict:
        """Durably select one request_id per exact publication intent before any network call.

        A retry of the same image, Hub, name, visibility, manifest and source
        mode reuses the newest generation's request_id so the Hub can replay
        its idempotent result. "Newest" means the newest row visible to ``credential``
        (see ``intent_publications``): another credential's unresolved attempt is never
        reused. A new generation (fresh request_id) is created only when there is none,
        the newest is terminal, or the newest is still exactly ``stale_request_id`` (a
        published row that no longer revalidates). If another writer already moved past
        that stale row, its newer row is reused instead of inserting a second one.
        ``inherit_hub_request_id`` makes a new generation send that Hub request id
        (the one of an own published row whose replay could not be confirmed) so the Hub
        answers an already committed image instead of registering another. The returned
        record carries ``prior_error_code``, the outcome recorded before this retry.
        """
        key, closure = self._intent(image_id, hub_url, name, visibility, manifest_digest, source_closure)
        now = validate_catalog_text(now)
        credential = _credential_id(credential)
        if stale_request_id is not None:
            stale_request_id = _uuid_text(stale_request_id)
        if inherit_hub_request_id is not None:
            inherit_hub_request_id = _uuid_text(inherit_hub_request_id)
        with self._write():
            upgraded = self._upgrade()
            image = self._connection.execute(
                "SELECT materialization_complete,source_binding_verified FROM images WHERE image_id=?", (key[0],)
            ).fetchone()
            if image is None:
                raise CatalogError("local image is not registered in the selected catalog")
            if tuple(image) != (1, 1):
                raise CatalogError("local image is not completely materialized from verified source bytes")
            row = self._connection.execute(
                "SELECT request_id,status,generation,terminal,error_code,source_closure FROM publications WHERE image_id=? AND hub_url=? AND name=? AND visibility=? AND manifest_digest=? AND source_mode=? AND (status='published' OR hub_credential IS ?) ORDER BY generation DESC LIMIT 1",
                (*key, credential),
            ).fetchone()
            own_attempt = self._connection.execute(
                "SELECT request_id,status,generation,terminal,error_code,source_closure FROM publications WHERE image_id=? AND hub_url=? AND name=? AND visibility=? AND manifest_digest=? AND source_mode=? AND hub_credential IS ? AND status<>'published' AND terminal=0 ORDER BY generation DESC LIMIT 1",
                (*key, credential),
            ).fetchone()
            if own_attempt is not None:
                # This credential's own unresolved attempt always wins over a newer published row of another
                # credential: its request id may already be committed on the Hub.
                row = own_attempt
            prior = None
            if row is None or row[3] or (stale_request_id is not None and row[0] == stale_request_id):
                request_id = str(uuid.uuid4())
                newest = self._connection.execute(
                    "SELECT MAX(generation) FROM publications WHERE image_id=? AND hub_url=? AND name=? AND visibility=? AND manifest_digest=? AND source_mode=?",
                    key,
                ).fetchone()[0]
                self._connection.execute(
                    "INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,0,NULL,NULL,NULL,?,?,'pending',NULL,?,?)",
                    (
                        request_id,
                        *key,
                        closure,
                        0 if newest is None else newest + 1,
                        inherit_hub_request_id,
                        credential,
                        now,
                        now,
                    ),
                )
            else:
                request_id, prior = row[0], row[4]
                if row[5] != closure:
                    raise CatalogError("catalog publication source closure differs for the same request id")
                if row[1] != "published":
                    # A retry is in flight again; an interrupted retry stays uncertain.
                    self._connection.execute(
                        "UPDATE publications SET status='pending',hub_image_id=NULL,hub_project_id=NULL,hub_created_by=NULL,error_code=NULL,updated_at=? WHERE request_id=?",
                        (now, request_id),
                    )
            selected = self._publication(request_id)
        if upgraded:
            self._version = _SCHEMA_VERSION
        return {**selected, "prior_error_code": prior}

    def mark_publication_terminal(self, request_id: str, *, now: str) -> dict:
        """End a request id (for example a Hub request_id conflict) without changing its status."""
        request_id, now = _uuid_text(request_id), validate_catalog_text(now)
        if self._version != _SCHEMA_VERSION:
            raise CatalogError("catalog publication was not found")
        with self._write():
            if self._publication(request_id) is None:
                raise CatalogError("catalog publication was not found")
            self._connection.execute(
                "UPDATE publications SET terminal=1,updated_at=? WHERE request_id=?", (now, request_id)
            )
            return self._publication(request_id)

    def rebind_publication(
        self,
        request_id: str,
        *,
        hub_image_id: str,
        hub_project_id: str,
        hub_created_by: str | None,
        now: str,
        hub_credential: str | None = None,
    ) -> dict:
        """Point a ``published`` row at the image the Hub now holds for its own (project, request id).

        Only the image id (and its creator/observer) may change; the project,
        generation and intent stay, so the unique key is untouched.
        """
        request_id, hub_image_id, now = _uuid_text(request_id), _uuid_text(hub_image_id), validate_catalog_text(now)
        _hub_identity(hub_project_id)
        if hub_created_by is not None:
            _hub_identity(hub_created_by)
        hub_credential = _credential_id(hub_credential)
        if self._version != _SCHEMA_VERSION:
            raise CatalogError("catalog publication was not found")
        with self._write():
            current = self._publication(request_id)
            if current is None or current["status"] != "published" or current["terminal"]:
                raise CatalogError("only a live published publication can be rebound")
            if current["hub_project_id"] != hub_project_id:
                raise CatalogError("a publication cannot be rebound to another project")
            self._connection.execute(
                "UPDATE publications SET hub_image_id=?,hub_created_by=?,hub_credential=COALESCE(?,hub_credential),updated_at=? WHERE request_id=?",
                (hub_image_id, hub_created_by, hub_credential, now, request_id),
            )
            return self._publication(request_id)

    def settle_publication(
        self,
        request_id: str,
        *,
        status: str,
        now: str,
        hub_image_id: str | None = None,
        hub_project_id: str | None = None,
        hub_created_by: str | None = None,
        error_code: str | None = None,
        terminal: bool = False,
        hub_credential: str | None = None,
    ) -> dict:
        """Record one observed outcome; ``published`` is final and requires the Hub identity."""
        request_id, now = _uuid_text(request_id), validate_catalog_text(now)
        if status not in ("published", "failed", "unknown") or type(terminal) is not bool:
            raise CatalogError("catalog publication status is invalid")
        if hub_image_id is not None:
            hub_image_id = _uuid_text(hub_image_id)
        for value in (hub_project_id, hub_created_by):
            if value is not None:
                _hub_identity(value)
        hub_credential = _credential_id(hub_credential)
        if status == "published" and (
            hub_image_id is None or hub_project_id is None or error_code is not None or terminal
        ):
            raise CatalogError("published status requires exactly the authoritative Hub image id and project")
        if error_code is not None and (not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code)):
            raise CatalogError("catalog publication error code is invalid")
        if self._version != _SCHEMA_VERSION:
            raise CatalogError("catalog publication was not found")
        with self._write():
            current = self._publication(request_id)
            if current is None:
                raise CatalogError("catalog publication was not found")
            if current["status"] == "published" and (
                status != "published"
                or (current["hub_image_id"], current["hub_project_id"]) != (hub_image_id, hub_project_id)
            ):
                raise CatalogError("a published publication status is immutable")
            self._connection.execute(
                "UPDATE publications SET status=?,hub_image_id=?,hub_project_id=?,hub_created_by=?,hub_credential=?,error_code=?,terminal=?,updated_at=? WHERE request_id=?",
                (
                    status,
                    hub_image_id,
                    hub_project_id,
                    hub_created_by,
                    hub_credential,
                    error_code,
                    int(terminal),
                    now,
                    request_id,
                ),
            )
            selected = self._publication(request_id)
        return selected

    def read_publications(self, *, image_id: str | None = None) -> list[dict]:
        self._check()
        if self._version != _SCHEMA_VERSION:
            return []
        query = "SELECT request_id FROM publications"
        arguments: tuple = ()
        if image_id is not None:
            query, arguments = query + " WHERE image_id=?", (_digest(image_id),)
        return [
            self._publication(row[0])
            for row in self._connection.execute(query + " ORDER BY created_at,generation,request_id", arguments)
        ]

    def _pulled(self, hub_url: str, hub_image_id: str) -> dict | None:
        row = self._connection.execute(
            f"SELECT {','.join(_PULLED_FIELDS)} FROM pulled_images WHERE hub_url=? AND hub_image_id=?",
            (hub_url, hub_image_id),
        ).fetchone()
        if row is None:
            return None
        record = dict(zip(_PULLED_FIELDS, row, strict=True))
        record["manifest"] = json.loads(record["manifest"])
        record["source_closure"] = json.loads(record["source_closure"])
        record["layers"] = [
            dict(zip(("id", "parent_id", "digest", "size_bytes"), layer, strict=True))
            for layer in self._connection.execute(
                "SELECT node_id,parent_id,digest,size_bytes FROM pulled_layers WHERE hub_url=? AND hub_image_id=? ORDER BY ordinal",
                (hub_url, hub_image_id),
            )
        ]
        return record

    def register_pulled_image(self, record: dict) -> tuple[dict, bool]:
        """Register verified Hub-published bytes as source ``published``; never overwrite.

        Returns the stored record and whether it was newly created. Content
        whose verifiable facts (identity, chain, closure) equal the stored row
        is idempotent; any differing fact for the same Hub image id is refused.
        """
        if not isinstance(record, dict) or set(record) != {*_PULLED_FIELDS, "layers"} - {"source"}:
            raise CatalogError("catalog pulled image shape is invalid")
        hub_url, hub_image_id = validate_hub_url(record["hub_url"]), _uuid_text(record["hub_image_id"])
        name, pulled_at = _publication_name(record["name"]), validate_catalog_text(record["pulled_at"])
        if record["visibility"] not in _VISIBILITIES:
            raise CatalogError("catalog pulled image visibility is invalid")
        manifest_digest, leaf_id = _digest(record["manifest_digest"]), _digest(record["leaf_id"])
        mode, platform = validate_catalog_text(record["mode"]), validate_catalog_text(record["platform"])
        if not isinstance(record["manifest"], dict):
            raise CatalogError("catalog pulled manifest is invalid")
        closure = _source_closure(record["source_closure"]) if record["source_closure"] else []
        layout = record["source_layout"]
        if (layout is None) != (not closure) or (
            layout is not None
            and (
                not isinstance(layout, str)
                or layout.startswith("/")
                or any(part in ("", ".", "..") for part in layout.split("/"))
            )
        ):
            raise CatalogError("catalog pulled source layout is invalid")
        local_image_id = _digest(record["local_image_id"], optional=True)
        layers = record["layers"]
        if not isinstance(layers, list) or not 1 <= len(layers) <= 128:
            raise CatalogError("catalog pulled layer count is invalid")
        rows, parent = [], None
        for ordinal, layer in enumerate(layers):
            if not isinstance(layer, dict) or set(layer) != {"id", "parent_id", "digest", "size_bytes"}:
                raise CatalogError("catalog pulled layer is invalid")
            size = layer["size_bytes"]
            if layer["parent_id"] != parent or type(size) is not int or not 0 < size < 2**63:
                raise CatalogError("catalog pulled layer chain is invalid")
            rows.append((hub_url, hub_image_id, ordinal, _digest(layer["id"]), parent, _digest(layer["digest"]), size))
            parent = layer["id"]
        if parent != leaf_id:
            raise CatalogError("catalog pulled leaf does not match its layer chain")
        values = {
            "hub_url": hub_url,
            "hub_image_id": hub_image_id,
            "name": name,
            "visibility": record["visibility"],
            "manifest_digest": manifest_digest,
            "leaf_id": leaf_id,
            "mode": mode,
            "platform": platform,
            "source": "published",
            "manifest": _json(record["manifest"]),
            "source_closure": _json(closure),
            "source_layout": layout,
            "local_image_id": local_image_id,
            "pulled_at": pulled_at,
        }
        layer_values = [dict(zip(("id", "parent_id", "digest", "size_bytes"), row[3:], strict=True)) for row in rows]
        with self._write():
            upgraded = self._upgrade()
            existing = self._pulled(hub_url, hub_image_id)
            created = existing is None
            if created:
                self._connection.execute(
                    "INSERT INTO pulled_images VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(values[field] for field in _PULLED_FIELDS),
                )
                self._connection.executemany("INSERT INTO pulled_layers VALUES(?,?,?,?,?,?,?)", rows)
            else:
                observed = {**values, "source_closure": closure}
                if (
                    any(existing[field] != observed[field] for field in _PULLED_COMPARED)
                    or existing["layers"] != layer_values
                ):
                    raise CatalogError(
                        "a different image is already registered for this Hub image id; refusing to overwrite"
                    )
                if existing["local_image_id"] is None and local_image_id is not None:
                    # A later pull may complete the source registration the first one lacked.
                    self._connection.execute(
                        "UPDATE pulled_images SET local_image_id=? WHERE hub_url=? AND hub_image_id=?",
                        (local_image_id, hub_url, hub_image_id),
                    )
            stored = self._pulled(hub_url, hub_image_id)
        if upgraded:
            self._version = _SCHEMA_VERSION
        return stored, created

    def read_pulled_images(self) -> list[dict]:
        self._check()
        if self._version != _SCHEMA_VERSION:
            return []
        return [
            self._pulled(hub_url, hub_image_id)
            for hub_url, hub_image_id in self._connection.execute(
                "SELECT hub_url,hub_image_id FROM pulled_images ORDER BY hub_url,hub_image_id"
            ).fetchall()
        ]

    def rebind_destination(self, *, source_state: Path) -> None:
        if self._rebind_from != _canonical_state(source_state):
            raise CatalogError("catalog relocation requires its exact source binding")
        with self._write():
            for table in ("derived_layers", "artifacts", "builds", "runs", "observation"):
                self._connection.execute(f"DELETE FROM {table}")
            self._connection.execute(
                "UPDATE images SET source_cas_id=NULL,source_binding_verified=0,materialization_complete=0"
            )
            self._connection.execute(
                "UPDATE catalog SET catalog_id=?,root_binding=?,revision=0,captured_at=NULL,warnings=? WHERE singleton=1",
                (str(uuid.uuid4()), self._files.binding(), "[]"),
            )
        self._rebind_from = None


def _remove_failed_initialization(files: _Files, *, journal_was_present: bool) -> None:
    """Remove only this unopened-to-callers initialization's fresh private inode."""
    try:
        files.check()
        if not journal_was_present:
            try:
                os.stat("metadata.sqlite3-journal", dir_fd=files.directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                journal_fd, _ = _file(files.directory_fd, "metadata.sqlite3-journal", create=False)
                try:
                    if _identity(os.fstat(journal_fd)) != _identity(
                        os.stat("metadata.sqlite3-journal", dir_fd=files.directory_fd, follow_symlinks=False)
                    ):
                        raise CatalogError("fresh catalog journal identity changed")
                    os.unlink("metadata.sqlite3-journal", dir_fd=files.directory_fd)
                finally:
                    os.close(journal_fd)
        files.check()
        os.unlink("metadata.sqlite3", dir_fd=files.directory_fd)
        os.fsync(files.directory_fd)
    except (CatalogError, OSError):
        raise CatalogError("new catalog initialization cleanup could not be safely and durably completed") from None


def _connect(files: _Files, *, create: bool, timeout_seconds: float, rebind_from: Path | None) -> Catalog:
    connection = None
    fresh = False
    journal_was_present = False
    try:
        try:
            os.stat("metadata.sqlite3-journal", dir_fd=files.directory_fd, follow_symlinks=False)
            journal_was_present = True
        except FileNotFoundError:
            pass
        files.db_fd, fresh = _file(files.directory_fd, "metadata.sqlite3", create=create)
        if fresh and journal_was_present:
            raise CatalogError("new catalog database has a preexisting journal")
        files.check()
        connection = sqlite3.connect(
            files.roots.metadata_db.as_uri() + "?mode=rw", uri=True, timeout=timeout_seconds, isolation_level=None
        )
        connection.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA fullfsync=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        files.check()
        if fresh:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            connection.execute("INSERT INTO catalog VALUES(1,?,?,0,NULL,?)", (str(uuid.uuid4()), files.binding(), "[]"))
            connection.execute("COMMIT")
            os.fsync(files.directory_fd)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID or version not in (
            1,
            _SCHEMA_VERSION,
        ):
            raise CatalogError("catalog schema version or application identity is unsupported")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise CatalogError("catalog journal mode is unsupported")
        actual = {row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        # A version-1 catalog stays readable unchanged; only a publication/pull
        # write upgrades it, inside that write's own transaction.
        if actual != set(_SCHEMA if version == _SCHEMA_VERSION else _SCHEMA_V1):
            raise CatalogError("catalog schema differs from its registered version")
        if (
            connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
            or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise CatalogError("catalog integrity check failed")
        rows = connection.execute("SELECT * FROM catalog").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or str(uuid.UUID(rows[0][1])) != rows[0][1]:
            raise CatalogError("catalog identity record is invalid")
        catalog = Catalog(connection, files, rebind_from=rebind_from)
        catalog._version = version
        catalog._check()
        return catalog
    except BaseException:
        try:
            if connection is not None:
                connection.close()
            if fresh and files.db_fd is not None:
                _remove_failed_initialization(files, journal_was_present=journal_was_present)
        finally:
            if files.db_fd is not None:
                os.close(files.db_fd)
                files.db_fd = None
        raise


@contextmanager
def open_catalog(
    roots: StatePaths, *, create: bool = True, timeout_seconds: float = 5.0, _rebind_from: Path | None = None
) -> Iterator[Catalog | None]:
    """Hold the writer mutex, but no SQL transaction, while collecting source facts."""
    catalog = None
    try:
        if _rebind_from is not None:
            _rebind_from = _canonical_state(_rebind_from)
        with _locked(roots, create=create, timeout_seconds=timeout_seconds) as files:
            if files is None:
                yield None
                return
            catalog = _connect(files, create=create, timeout_seconds=timeout_seconds, rebind_from=_rebind_from)
            try:
                yield catalog
            finally:
                try:
                    catalog.close()
                finally:
                    if files.db_fd is not None:
                        os.close(files.db_fd)
                        files.db_fd = None
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, OSError):
        raise CatalogError("catalog metadata operation failed; retained state was not rebuilt") from None


@contextmanager
def quiesce_catalog(roots: StatePaths, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Validate and close SQLite while retaining its writer mutex for root copying."""
    try:
        with _locked(roots, create=False, timeout_seconds=timeout_seconds, check_exit=False) as files:
            if files is not None:
                catalog = _connect(files, create=False, timeout_seconds=timeout_seconds, rebind_from=None)
                try:
                    catalog.close()
                finally:
                    if files.db_fd is not None:
                        os.close(files.db_fd)
                        files.db_fd = None
            yield None
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, OSError):
        raise CatalogError("catalog cannot be quiesced safely") from None
