"""Permanent operator-owned OCI source and SquashFS cache on mounted storage.

The catalog contains portable content identities, never SourceCAS or OCIStore
receipts. Those authorities are reconstructed and verified on the current host.
No operation here removes source or derived bytes, including release.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from . import registry_intake
from .errors import ArtifactValidationError
from .oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID
from .oci_image import OCIImageRef, resolve_image, strict_json_object
from .oci_materializer import OCIHardWorkerError, materialize_image_hard
from .oci_packer import (
    SQUASHFS_PACK_POLICY_ID,
    SquashFSPackProfile,
    discover_squashfs_toolchain,
)
from .oci_provenance import Descriptor, Platform
from .oci_source import (
    LocalArchiveSource,
    LocalLayoutSource,
    SnapshotStage,
    SnapshottedOCIImage,
    SourceCAS,
    SourceSnapshot,
)
from .oci_store import DerivedLayerOccurrence, DerivedSquashFSKey, OCIStore
from .state import StatePaths

SCHEMA = "palimpsest.layer-cache.v1"
_MARKER = "layer-cache.json"
_LOCK = ".layer-cache.lock"
_MAX_CATALOG_BYTES = 32 * 1024 * 1024
_CONSUMER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class LayerCacheError(ArtifactValidationError):
    """A cache prerequisite, binding, or retained content is invalid."""


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise LayerCacheError("layer cache requires a canonical sha256 digest")
    return value


def _consumer(value: Any) -> str:
    if not isinstance(value, str) or _CONSUMER.fullmatch(value) is None:
        raise LayerCacheError("layer cache consumer must be a clean 1-128 character identifier")
    return value


def _signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode)


def _stable(value: os.stat_result) -> tuple[int, ...]:
    return (*_signature(value), value.st_mode, value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _private_directory(fd: int) -> tuple[int, int, int, int]:
    value = os.fstat(fd)
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o700:
        raise LayerCacheError("layer cache directories must be operator-owned physical mode 0700 directories")
    return _signature(value)


def _trusted_ancestor(fd: int) -> None:
    metadata = os.fstat(fd)
    if metadata.st_uid not in {0, os.geteuid()} or (
        stat.S_IMODE(metadata.st_mode) & 0o022 and not metadata.st_mode & stat.S_ISVTX
    ):
        raise LayerCacheError("layer cache ancestors must prevent replacement by untrusted users")


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    """Pin every path component, rejecting symlinks, including ancestor aliases."""
    if not path.is_absolute() or ".." in path.parts or "\0" in os.fspath(path):
        raise LayerCacheError("layer cache paths must be canonical absolute paths")
    with ExitStack() as stack:
        fd = os.open("/", _DIR_FLAGS)
        stack.callback(os.close, fd)
        bindings: list[tuple[int, str, int]] = []
        for component in path.parts[1:]:
            _trusted_ancestor(fd)
            child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            stack.callback(os.close, child)
            bindings.append((fd, component, child))
            fd = child
        _trusted_ancestor(fd)
        yield fd
        for parent, name, child in bindings:
            visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _signature(visible) != _signature(os.fstat(child)) or not stat.S_ISDIR(visible.st_mode):
                raise LayerCacheError("layer cache path changed during operation")


def _read_file(parent: int, name: str, *, mode: int, maximum: int) -> bytes:
    fd = os.open(name, _READ_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size > maximum
        ):
            raise LayerCacheError("layer cache metadata is not a private bounded regular file")
        parts = []
        remaining = maximum + 1
        while remaining:
            part = os.read(fd, min(1024 * 1024, remaining))
            if not part:
                break
            parts.append(part)
            remaining -= len(part)
        payload = b"".join(parts)
        if (
            len(payload) != before.st_size
            or _stable(before) != _stable(os.fstat(fd))
            or _stable(before) != _stable(os.stat(name, dir_fd=parent, follow_symlinks=False))
        ):
            raise LayerCacheError("layer cache metadata changed during read")
        return payload
    finally:
        os.close(fd)


def _descriptor(value: Any) -> Descriptor:
    if not isinstance(value, dict) or set(value) != {"mediaType", "digest", "size"}:
        raise LayerCacheError("layer cache descriptor is malformed")
    if type(value["size"]) is not int or not 0 <= value["size"] <= 2**63 - 1:
        raise LayerCacheError("layer cache descriptor size is invalid")
    descriptor = Descriptor(value["mediaType"], _digest(value["digest"]), value["size"])
    if descriptor.to_dict() != value:
        raise LayerCacheError("layer cache descriptor is not canonical")
    return descriptor


def _reference(root_digest: str) -> OCIImageRef:
    return OCIImageRef(
        "local.palimpsest.invalid", "layer-cache/image", f"local.palimpsest.invalid/layer-cache/image@{root_digest}"
    )


def _image_record(snapshot: SnapshottedOCIImage) -> dict[str, Any]:
    return {
        "root_descriptor": snapshot.root.descriptor.to_dict(),
        "manifest_descriptor": snapshot.manifest.descriptor.to_dict(),
        "config_descriptor": snapshot.config.descriptor.to_dict(),
        "platform": snapshot.image.platform.to_dict(),
        "layers": [layer.to_dict() for layer in snapshot.image.layers],
    }


def _missing_derived():
    raise LayerCacheError("retained layer cache recipe or SquashFS bytes are missing or corrupt")


class LayerCache:
    """One private, already-mounted cache; flock must work across all writers."""

    def __init__(self, root: Path, *, initialize: bool = False):
        if not isinstance(root, Path):
            raise LayerCacheError("layer cache root must be a Path")
        if type(initialize) is not bool:
            raise LayerCacheError("layer cache initialization policy must be boolean")
        self.root = root.expanduser().absolute()
        if self.root == Path("/") or ".." in self.root.parts:
            raise LayerCacheError("layer cache root must be a canonical private directory")
        self.roots = StatePaths(self.root / "config", self.root / "state")
        self._bindings: dict[str, tuple[int, int, int, int]] = {}
        self._cache_id: str | None = None
        with self._locked(initialize=initialize) as (state_fd, fresh):
            if fresh:
                catalog = {
                    "schema": SCHEMA,
                    "cache_id": str(uuid.uuid4()),
                    "images": {},
                    "references": {},
                    "consumers": {},
                    "derived": {},
                }
                self._save(state_fd, catalog)
                os.mkdir("runtime-packs", 0o700, dir_fd=state_fd)
                os.fsync(state_fd)
            else:
                catalog = self._load(state_fd)
            self._cache_id = catalog["cache_id"]
            self._source = SourceCAS(self.roots.oci_source_cas)
            self._store = OCIStore(self.roots)
            self._pin_storage()

    def _pin_storage(self) -> None:
        # Pin existing authorities before constructors may create missing children.
        paths = [self.roots.runtime_packs, self.roots.oci_source_cas, self.roots.store, self.roots.oci_derived_store]
        paths += [self.roots.oci_source_cas / name for name in ("blobs", "blobs/sha256", "locks")]
        paths += [self.roots.store / name for name in ("blobs", "blobs/sha256", "oci-artifact-locks-v1")]
        paths += [
            self.roots.oci_derived_store / name
            for name in ("records", "keys", "occurrences", "leases", "lease-sets", "locks")
        ]
        for path in paths:
            with _directory(path) as fd:
                signature = _private_directory(fd)
                key = os.fspath(path.relative_to(self.root))
                if key in self._bindings and self._bindings[key] != signature:
                    raise LayerCacheError("layer cache storage authority changed")
                self._bindings[key] = signature

    @contextmanager
    def _locked(self, *, initialize: bool = False) -> Iterator[tuple[int, bool]]:
        try:
            with _directory(self.root) as root_fd, ExitStack() as stack:
                signature = _private_directory(root_fd)
                if self._bindings.get(".", signature) != signature:
                    raise LayerCacheError("layer cache mounted root changed")
                self._bindings["."] = signature
                entries_before_lock = set(os.listdir(root_fd))
                if _LOCK not in entries_before_lock and (entries_before_lock or not initialize):
                    raise LayerCacheError("existing layer cache lock is missing")
                fd = os.open(
                    _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=root_fd
                )
                stack.callback(os.close, fd)
                lock = os.fstat(fd)
                if (
                    not stat.S_ISREG(lock.st_mode)
                    or lock.st_uid != os.geteuid()
                    or lock.st_nlink != 1
                    or stat.S_IMODE(lock.st_mode) != 0o600
                ):
                    raise LayerCacheError("layer cache lock is unsafe")
                if getattr(self, "_lock_signature", _signature(lock)) != _signature(lock):
                    raise LayerCacheError("layer cache lock authority changed")
                self._lock_signature = _signature(lock)
                fcntl.flock(fd, fcntl.LOCK_EX)

                def verify_lock():
                    if _stable(os.stat(_LOCK, dir_fd=root_fd, follow_symlinks=False)) != _stable(lock) or _stable(
                        os.fstat(fd)
                    ) != _stable(lock):
                        raise LayerCacheError("layer cache lock changed")

                verify_lock()
                entries = set(os.listdir(root_fd))
                fresh = initialize and entries == {_LOCK}
                opened = {}
                for name in ("config", "state"):
                    if fresh:
                        os.mkdir(name, 0o700, dir_fd=root_fd)
                        os.fsync(root_fd)
                    child = os.open(name, _DIR_FLAGS, dir_fd=root_fd)
                    stack.callback(os.close, child)
                    current = _private_directory(child)
                    if self._bindings.get(name, current) != current:
                        raise LayerCacheError("layer cache state authority changed")
                    self._bindings[name] = current
                    opened[name] = child
                if not fresh and initialize:
                    # Never repair/recreate a missing retained storage component.
                    self._pin_storage()
                elif not initialize:
                    self._pin_storage()
                yield opened["state"], fresh
                verify_lock()
                for name, child in opened.items():
                    if _signature(os.stat(name, dir_fd=root_fd, follow_symlinks=False)) != _private_directory(child):
                        raise LayerCacheError("layer cache state path changed")
                if hasattr(self, "_source"):
                    self._pin_storage()
        except OSError as exc:
            raise LayerCacheError(
                "layer cache requires an existing safe mounted root and intact private storage"
            ) from exc

    def _load(self, state_fd: int) -> dict[str, Any]:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise LayerCacheError("layer cache catalog contains duplicate keys")
                result[key] = value
            return result

        try:
            before = _stable(os.stat(_MARKER, dir_fd=state_fd, follow_symlinks=False))
            catalog = json.loads(
                _read_file(state_fd, _MARKER, mode=0o600, maximum=_MAX_CATALOG_BYTES), object_pairs_hook=pairs
            )
            self._catalog_signature = _stable(os.stat(_MARKER, dir_fd=state_fd, follow_symlinks=False))
            if self._catalog_signature != before:
                raise LayerCacheError("layer cache catalog changed during read")
            if (
                not isinstance(catalog, dict)
                or set(catalog) != {"schema", "cache_id", "images", "references", "consumers", "derived"}
                or catalog["schema"] != SCHEMA
                or str(uuid.UUID(catalog["cache_id"])) != catalog["cache_id"]
                or self._cache_id not in (None, catalog["cache_id"])
            ):
                raise LayerCacheError("layer cache catalog identity is invalid")
            for key in ("images", "references", "consumers", "derived"):
                if not isinstance(catalog[key], dict):
                    raise LayerCacheError("layer cache catalog map is invalid")
            for root, record in catalog["images"].items():
                _digest(root)
                if (
                    not isinstance(record, dict)
                    or set(record)
                    != {"root_descriptor", "manifest_descriptor", "config_descriptor", "platform", "layers"}
                    or _descriptor(record["root_descriptor"]).digest != root
                    or record["platform"] != Platform(os="linux", architecture="amd64").to_dict()
                    or not isinstance(record["layers"], list)
                    or not record["layers"]
                ):
                    raise LayerCacheError("layer cache image record is invalid")
                _descriptor(record["manifest_descriptor"])
                _descriptor(record["config_descriptor"])
                for ordinal, layer in enumerate(record["layers"]):
                    if (
                        not isinstance(layer, dict)
                        or set(layer) != {"compressed", "diff_id", "ordinal"}
                        or type(layer["ordinal"]) is not int
                        or layer["ordinal"] != ordinal
                    ):
                        raise LayerCacheError("layer cache layer order is invalid")
                    _descriptor(layer["compressed"])
                    _digest(layer["diff_id"])
            for reference, root in catalog["references"].items():
                resolved = registry_intake.resolve_anonymous_reference(reference)
                if resolved.canonical != reference or root not in catalog["images"]:
                    raise LayerCacheError("layer cache reference record is invalid")
                if "@" in reference and reference.rsplit("@", 1)[1] != root:
                    raise LayerCacheError("layer cache immutable reference contradicts its pinned root")
            for consumer, root in catalog["consumers"].items():
                _consumer(consumer)
                if root not in catalog["images"]:
                    raise LayerCacheError("layer cache consumer references an absent image")
            sources = {
                (
                    layer["compressed"]["mediaType"],
                    layer["compressed"]["digest"],
                    layer["compressed"]["size"],
                    layer["diff_id"],
                )
                for image in catalog["images"].values()
                for layer in image["layers"]
            }
            for digest, record in catalog["derived"].items():
                if not isinstance(record, dict) or set(record) != {
                    "key",
                    "key_digest",
                    "compressed_digest",
                    "diff_id",
                    "image_digest",
                    "image_size",
                }:
                    raise LayerCacheError("layer cache derived record is malformed")
                key = DerivedSquashFSKey.from_dict(record["key"])
                if (key.source_media_type, key.compressed_digest, key.compressed_size, key.diff_id) not in sources:
                    raise LayerCacheError("layer cache derived recipe has no retained source")
                if (
                    digest != key.digest
                    or record["key_digest"] != digest
                    or record["compressed_digest"] != key.compressed_digest
                    or record["diff_id"] != key.diff_id
                    or type(record["image_size"]) is not int
                    or record["image_size"] <= 0
                ):
                    raise LayerCacheError("layer cache derived record is inconsistent")
                _digest(record["image_digest"])
            return catalog
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise LayerCacheError("layer cache catalog is malformed") from exc

    def _save(self, state_fd: int, catalog: dict[str, Any]) -> None:
        payload = json.dumps(catalog, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(payload) > _MAX_CATALOG_BYTES:
            raise LayerCacheError("layer cache catalog exceeds its size bound")
        temporary = f".layer-cache-{uuid.uuid4().hex}"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=state_fd
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise LayerCacheError("layer cache catalog write made no progress")
                view = view[written:]
            os.fsync(fd)
            expected = getattr(self, "_catalog_signature", None)
            try:
                visible = _stable(os.stat(_MARKER, dir_fd=state_fd, follow_symlinks=False))
            except FileNotFoundError:
                visible = None
            if visible != expected:
                raise LayerCacheError("layer cache catalog changed before publication")
            os.replace(temporary, _MARKER, src_dir_fd=state_fd, dst_dir_fd=state_fd)
            os.fsync(state_fd)
            self._catalog_signature = _stable(os.stat(_MARKER, dir_fd=state_fd, follow_symlinks=False))
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=state_fd)
            except FileNotFoundError:
                pass

    def _snapshot(self, descriptor: Descriptor) -> SnapshottedOCIImage:
        def member(value):
            return SourceSnapshot(value, self._source.identity)

        image = resolve_image(
            _reference(descriptor.digest), descriptor, lambda value: self._source.read_metadata(member(value))
        )
        snapshot = SnapshottedOCIImage(
            image,
            member(descriptor),
            member(image.manifest_descriptor),
            member(image.config.descriptor),
            tuple(member(layer.compressed) for layer in image.layers),
        )
        self._source.verify_image(snapshot)
        return snapshot

    def _verified(self, catalog: dict[str, Any], root: str) -> SnapshottedOCIImage:
        _digest(root)
        record = catalog["images"].get(root)
        if record is None:
            raise LayerCacheError("image root is not present in the layer cache")
        snapshot = self._snapshot(_descriptor(record["root_descriptor"]))
        if _image_record(snapshot) != record:
            raise LayerCacheError("layer cache image record contradicts its verified source graph")
        recipes = [(DerivedSquashFSKey.from_dict(value["key"]), value) for value in catalog["derived"].values()]
        for ordinal in range(len(snapshot.layers)):
            occurrence = DerivedLayerOccurrence.from_image(snapshot, ordinal)
            for key, derived in recipes:
                if key.matches(occurrence):
                    receipt = self._store.materialize(occurrence, key, _missing_derived)
                    if receipt.image_digest != derived["image_digest"] or receipt.image_size != derived["image_size"]:
                        raise LayerCacheError("layer cache derived catalog contradicts the verified recipe")
        return snapshot

    def _check_retained_files(self, catalog: dict[str, Any]) -> set[str]:
        """Audit catalog targets without rereading unrelated compressed payloads."""
        sources: dict[str, int] = {}
        for image in catalog["images"].values():
            descriptors = [image[name] for name in ("root_descriptor", "manifest_descriptor", "config_descriptor")]
            descriptors.extend(layer["compressed"] for layer in image["layers"])
            for descriptor in descriptors:
                digest, size = descriptor["digest"], descriptor["size"]
                if sources.get(digest, size) != size:
                    raise LayerCacheError("retained source descriptors contradict one another")
                sources[digest] = size
        derived = {value["image_digest"]: value["image_size"] for value in catalog["derived"].values()}
        for directory, records in ((self.roots.oci_source_cas, sources), (self.roots.store, derived)):
            with _directory(directory / "blobs" / "sha256") as blobs_fd:
                for digest, size in records.items():
                    metadata = os.stat(digest.split(":")[1], dir_fd=blobs_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.geteuid()
                        or metadata.st_nlink != 1
                        or stat.S_IMODE(metadata.st_mode) != 0o400
                        or metadata.st_size != size
                    ):
                        raise LayerCacheError("retained layer cache blob metadata is invalid")
        return set(sources)

    def _view(self, catalog: dict[str, Any], root: str | None = None) -> dict[str, Any]:
        result = {**catalog, "root": os.fspath(self.root), "retention": "permanent"}
        if root is not None:
            record = catalog["images"][root]
            result.update(
                root_digest=root,
                image=record,
                consumer_ids=sorted(name for name, digest in catalog["consumers"].items() if digest == root),
                source_blob_paths=[
                    os.fspath(self.roots.oci_source_cas / "blobs" / "sha256" / digest.split(":")[1])
                    for digest in dict.fromkeys(
                        [
                            root,
                            record["manifest_descriptor"]["digest"],
                            record["config_descriptor"]["digest"],
                            *(layer["compressed"]["digest"] for layer in record["layers"]),
                        ]
                    )
                ],
            )
            sources = {(layer["compressed"]["digest"], layer["diff_id"]) for layer in record["layers"]}
            result["derived_blob_paths"] = sorted(
                {
                    os.fspath(self.roots.store / "blobs" / "sha256" / derived["image_digest"].split(":")[1])
                    for derived in catalog["derived"].values()
                    if (derived["compressed_digest"], derived["diff_id"]) in sources
                }
            )
        return result

    def import_source(self, source: Path, *, manifest_digest: str | None = None) -> dict[str, Any]:
        source = source.expanduser().absolute()
        if manifest_digest is not None:
            _digest(manifest_digest)
        with self._locked() as (fd, _fresh):
            catalog = self._load(fd)
            retained = self._check_retained_files(catalog)

            def refuse_repair(checkpoint):
                if checkpoint.stage == SnapshotStage.BEFORE_PROMOTE and checkpoint.digest in retained:
                    raise LayerCacheError("retained source blob is corrupt; cache import cannot repair it")

            transport_type = LocalLayoutSource if source.is_dir() else LocalArchiveSource
            transport = transport_type(source, manifest_digest, _checkpoint=refuse_repair)
            imported = transport.snapshot(None, self._source)
            root = imported.root.descriptor.digest
            snapshot = self._snapshot(imported.root.descriptor)
            record = _image_record(snapshot)
            if root in catalog["images"]:
                self._verified(catalog, root)
            catalog["images"][root] = record
            self._save(fd, catalog)
            return self._view(catalog, root)

    def pull(
        self, reference: str, *, offline: bool = False, timeout_seconds: float = 300.0, skopeo: Path | None = None
    ) -> dict[str, Any]:
        if type(offline) is not bool:
            raise LayerCacheError("layer cache offline option must be boolean")
        resolved = registry_intake.resolve_anonymous_reference(reference)
        canonical = resolved.canonical
        pin = canonical.rsplit("@", 1)[1] if "@" in canonical else None
        with self._locked() as (fd, _fresh):
            catalog = self._load(fd)
            root = catalog["references"].get(canonical)
            if pin in catalog["images"]:
                root = pin
            if offline or (pin is not None and root is not None):
                if root is None:
                    raise LayerCacheError("reference is not available in the offline layer cache")
                self._verified(catalog, root)
                return self._view(catalog, root)
            # A mutable tag refreshes by default. Failure never falls back silently.
            self._check_retained_files(catalog)
            pulled = registry_intake.pull_anonymous_oci_layout(
                canonical, self.roots, timeout_seconds=timeout_seconds, skopeo=skopeo
            )
            with _directory(self.roots.oci_source_cas) as layout_fd:
                # The helper publishes one selected transport label, not history.
                document = strict_json_object(
                    _read_file(layout_fd, "index.json", mode=0o600, maximum=8 * 1024 * 1024),
                    "layer cache transport index",
                )
            entries = document.get("manifests")
            if (
                type(document.get("schemaVersion")) is not int
                or document["schemaVersion"] != 2
                or not isinstance(entries, list)
                or any(not isinstance(entry, dict) for entry in entries)
            ):
                raise LayerCacheError("layer cache transport index is malformed")
            matches = [
                entry
                for entry in entries
                if entry.get("digest") == pulled.root_digest
                and isinstance(entry.get("annotations"), dict)
                and entry["annotations"].get("org.opencontainers.image.ref.name") == "selected"
            ]
            if len(matches) != 1:
                raise LayerCacheError("registry layout does not declare exactly one selected root")
            entry = matches[0]
            if "data" in entry:
                raise LayerCacheError("layer cache root cannot use embedded descriptor data")
            descriptor = _descriptor({name: entry[name] for name in ("mediaType", "digest", "size")})
            root = descriptor.digest
            if pin is not None and pin != root:
                raise LayerCacheError("registry result does not match the requested immutable pin")
            if root in catalog["images"]:
                snapshot = self._verified(catalog, root)
                if snapshot.root.descriptor != descriptor:
                    raise LayerCacheError("registry root descriptor contradicts retained metadata")
            else:
                snapshot = self._snapshot(descriptor)
            catalog["images"][root] = _image_record(snapshot)
            catalog["references"][canonical] = root
            self._save(fd, catalog)
            return self._view(catalog, root)

    def materialize(
        self,
        root_digest: str,
        *,
        packer_path: Path = Path("/usr/bin/mksquashfs"),
        timeout_seconds: float = 300.0,
        pack_profile: SquashFSPackProfile = SquashFSPackProfile.CANONICAL,
    ) -> dict[str, Any]:
        if not isinstance(pack_profile, SquashFSPackProfile):
            raise LayerCacheError("layer cache pack profile is invalid")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) < float("inf")
        ):
            raise LayerCacheError("layer cache deadline is invalid")
        deadline = time.monotonic() + float(timeout_seconds)
        policy = pack_profile.policy
        with self._locked() as (fd, _fresh):
            catalog = self._load(fd)
            snapshot = self._verified(catalog, _digest(root_digest))
            if not isinstance(packer_path, Path):
                raise LayerCacheError("layer cache packer must be a Path")
            packer_path = packer_path.expanduser().resolve(strict=True)
            with _directory(packer_path.parent) as parent_fd:
                packer_fd = os.open(packer_path.name, _READ_FLAGS, dir_fd=parent_fd)
                try:
                    metadata = os.fstat(packer_fd)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise LayerCacheError("layer cache packer must be a regular file")
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: os.read(packer_fd, 1024 * 1024), b""):
                        digest.update(chunk)
                    if _stable(metadata) != _stable(os.fstat(packer_fd)):
                        raise LayerCacheError("layer cache packer changed while identifying it")
                finally:
                    os.close(packer_fd)
            toolchain = discover_squashfs_toolchain(
                packer_path, expected_packer_sha256=digest.hexdigest(), policy=policy
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OCIHardWorkerError("oci-worker-timeout", "layer cache materialization deadline expired")
            receipt = materialize_image_hard(
                snapshot,
                source_cas_root=self.roots.oci_source_cas,
                roots=self.roots,
                store=self._store,
                packer_path=packer_path,
                toolchain=toolchain,
                timeout_seconds=remaining,
                pack_profile=pack_profile,
            )
            for ordinal, result in enumerate(receipt.results):
                occurrence = DerivedLayerOccurrence.from_image(snapshot, ordinal)
                key = DerivedSquashFSKey.for_occurrence(
                    occurrence,
                    intake_policy_id=LAYER_INTAKE_POLICY_ID,
                    intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
                    pack_policy_id=SQUASHFS_PACK_POLICY_ID,
                    pack_policy_fingerprint=policy.fingerprint,
                    toolchain=toolchain,
                )
                value = {
                    "key": key.to_dict(),
                    "key_digest": key.digest,
                    "compressed_digest": key.compressed_digest,
                    "diff_id": key.diff_id,
                    "image_digest": result.receipt.image_digest,
                    "image_size": result.receipt.image_size,
                }
                if result.receipt.key_digest != key.digest or catalog["derived"].get(key.digest, value) != value:
                    raise LayerCacheError("materialization contradicts the retained recipe catalog")
                catalog["derived"][key.digest] = value
            self._save(fd, catalog)
            return {
                **self._view(catalog, root_digest),
                "receipt": receipt.to_dict(),
                "blob_paths": [
                    os.fspath(self.roots.store / "blobs" / "sha256" / result.receipt.image_digest.split(":")[1])
                    for result in receipt.results
                ],
            }

    def retain(self, root_digest: str, consumer: str) -> dict[str, Any]:
        _consumer(consumer)
        with self._locked() as (fd, _fresh):
            catalog = self._load(fd)
            self._verified(catalog, _digest(root_digest))
            if catalog["consumers"].get(consumer, root_digest) != root_digest:
                raise LayerCacheError("layer cache consumer is already bound to a different immutable root")
            if consumer not in catalog["consumers"]:
                catalog["consumers"][consumer] = root_digest
                self._save(fd, catalog)
            return self._view(catalog, root_digest)

    def release(self, consumer: str) -> dict[str, Any]:
        _consumer(consumer)
        with self._locked() as (fd, _fresh):
            catalog = self._load(fd)
            root = catalog["consumers"].get(consumer)
            if root is not None:
                self._verified(catalog, root)
                del catalog["consumers"][consumer]
                self._save(fd, catalog)
            return {**self._view(catalog, root), "consumer": consumer, "released": root is not None}

    def inspect(self, root_digest: str | None = None) -> dict[str, Any]:
        with self._locked() as (fd, _fresh):
            catalog = self._load(fd)
            for root in [_digest(root_digest)] if root_digest is not None else catalog["images"]:
                self._verified(catalog, root)
            return self._view(catalog, root_digest)
