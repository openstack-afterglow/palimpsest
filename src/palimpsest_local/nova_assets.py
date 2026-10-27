"""Permanent cloud asset identities and contextual layer graphs, not another blob store.

The operator mounts storage and creates the private catalog directory first. Payloads
remain in the canonical worker/Cinder CAS. Releasing a consumer never deletes bytes,
volumes or records. A read-only Cinder binding describes an already frozen filesystem;
this module never formats, attaches, detaches or grants access to a provider resource.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError
from .layer_cache import _directory, _private_directory, _read_file, _stable
from .oci_image import strict_json_object
from .oci_packer import DEFAULT_SQUASHFS_PACK_POLICY, verify_squashfs_fd

SCHEMA = "palimpsest.cloud-assets.v1"
ASSET_SCHEMA = "palimpsest.cloud-asset.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}\Z")
_SOURCE_KEYS = {
    "kind",
    "reference",
    "root_digest",
    "manifest_digest",
    "config_digest",
    "base_image_id",
    "base_image_digest",
    "recipe_digest",
    "source_commit",
    "source_context_digest",
    "source_dockerfile_digest",
    "source_build_result_digest",
    "context_digest",
    "dockerfile_digest",
    "build_result_digest",
    "handoff_digest",
}
_LAYER_KEYS = {"digest", "size", "compressed_digest", "diff_id", "recipe_digest"}
_MAXIMUM = 32 * 1024 * 1024


def _fail(message: str) -> None:
    raise ArtifactValidationError("cloud assets: " + message)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail(label + " must be a bounded clean identifier")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("a canonical sha256 digest is required")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _asset(project_id: str, arch: str, source: dict, layers: list[dict]) -> dict:
    _identifier(project_id, "project")
    if arch not in {"x86_64", "aarch64"}:
        _fail("target architecture is required, not inferred from the worker")
    if not isinstance(source, dict) or set(source) - _SOURCE_KEYS:
        _fail("source contains unsupported fields")
    if source.get("kind") not in {"nova-build", "oci-registry", "oci-build"}:
        _fail("source kind must be nova-build, oci-registry or oci-build")
    if source["kind"] != "oci-build" and set(source) & {
        "source_commit",
        "source_context_digest",
        "source_dockerfile_digest",
        "source_build_result_digest",
        "context_digest",
        "dockerfile_digest",
        "build_result_digest",
        "handoff_digest",
    }:
        _fail("build provenance fields require an OCI build source")
    for key, value in source.items():
        if key.endswith("digest"):
            _digest(value)
        elif not isinstance(value, str) or not value or len(value) > 2048 or any(c in value for c in "\x00\r\n"):
            _fail("source field is malformed")
    if source["kind"] == "oci-registry":
        if not {"reference", "root_digest", "manifest_digest", "config_digest"} <= source.keys():
            _fail("OCI source requires original root, selected manifest and config identity")
        # Only scheme-free registry references: never persist URL userinfo or signed queries.
        if any(c in source["reference"] for c in "?#") or "://" in source["reference"]:
            _fail("source reference must be a credential-free OCI reference")
    elif source["kind"] == "oci-build":
        required = {
            "kind",
            "root_digest",
            "manifest_digest",
            "config_digest",
            "source_commit",
            "source_context_digest",
            "source_dockerfile_digest",
            "source_build_result_digest",
            "context_digest",
            "dockerfile_digest",
            "build_result_digest",
            "handoff_digest",
        }
        if set(source) != required or re.fullmatch(r"[0-9a-f]{40}", source["source_commit"]) is None:
            _fail("OCI build source requires pinned source, paired build receipts and handoff identities")
    elif not {"base_image_id", "recipe_digest"} <= source.keys():
        _fail("Nova build source requires base image and recipe identity")
    if not isinstance(layers, list) or not 1 <= len(layers) <= 128:
        _fail("asset requires one to 128 ordered layer occurrences")
    nodes = []
    parent = None
    for ordinal, layer in enumerate(layers):
        if not isinstance(layer, dict) or not {"digest", "size"} <= layer.keys() or set(layer) - _LAYER_KEYS:
            _fail("layer fields are invalid")
        _digest(layer["digest"])
        if type(layer["size"]) is not int or layer["size"] < 96:
            _fail("layer size must describe a nontruncated SquashFS")
        for key in set(layer) - {"size"}:
            _digest(layer[key])
        if (
            source["kind"] in {"oci-registry", "oci-build"}
            and not {"compressed_digest", "diff_id", "recipe_digest"} <= layer.keys()
        ):
            _fail("OCI occurrences require compressed, DiffID and conversion recipe identities")
        node = {**layer, "ordinal": ordinal, "parent_occurrence": parent}
        # Context includes source: one physical blob may have different parents in two images.
        node["occurrence_id"] = _identity({"source": source, "node": node})
        parent = node["occurrence_id"]
        nodes.append(node)
    return {"schema": ASSET_SCHEMA, "project_id": project_id, "arch": arch, "source": source, "layers": nodes}


def verify_blob(path: Path, digest: str, size: int, *, normalized_oci: bool = True) -> dict:
    """Hash and structurally verify the same pinned inode; reject unsafe publication."""
    _digest(digest)
    path = path.expanduser().absolute()
    with _directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid not in {0, os.geteuid()}
                or before.st_mode & 0o022
                or before.st_size != size
            ):
                _fail("layer payload ownership, permissions or size is invalid")
            actual = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                actual.update(chunk)
            if "sha256:" + actual.hexdigest() != digest:
                _fail("layer payload hash does not match its receipt")
            verify_squashfs_fd(
                fd, size, DEFAULT_SQUASHFS_PACK_POLICY.max_image_bytes, require_zero_mkfs_time=normalized_oci
            )
            visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _stable(before) != _stable(os.fstat(fd)) or _stable(before) != _stable(visible):
                _fail("layer payload changed during verification")
            return {"path": os.fspath(path), "device": before.st_dev, "inode": before.st_ino}
        finally:
            os.close(fd)


class AssetCatalog:
    """Project-bound permanent records over externally retained canonical blobs."""

    def __init__(self, root: Path, project_id: str, *, initialize: bool = False):
        self.root = root.expanduser().absolute()
        self.project_id = _identifier(project_id, "project")
        self._root_identity = None
        with self._locked(initialize=initialize) as fd:
            try:
                self._load(fd)
            except FileNotFoundError:
                if not initialize or set(os.listdir(fd)) != {".assets.lock"}:
                    _fail("catalog is absent; explicit init on an empty mounted directory is required")
                self._save(
                    fd,
                    {
                        "schema": SCHEMA,
                        "project_id": project_id,
                        "assets": {},
                        "names": {},
                        "consumers": {},
                        "stores": {},
                    },
                )

    @contextmanager
    def _locked(self, *, initialize: bool = False):
        with _directory(self.root) as fd:
            identity = _private_directory(fd)
            if self._root_identity not in (None, identity):
                _fail("mounted catalog directory changed")
            self._root_identity = identity
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            if initialize and not os.listdir(fd):
                flags |= os.O_CREAT | os.O_EXCL
            lock = os.open(".assets.lock", flags, 0o600, dir_fd=fd)
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    _fail("catalog lock is unsafe")
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield fd
                if _stable(os.fstat(lock)) != _stable(os.stat(".assets.lock", dir_fd=fd, follow_symlinks=False)):
                    _fail("catalog lock changed")
            finally:
                os.close(lock)

    def _load(self, fd: int) -> dict:
        data = strict_json_object(_read_file(fd, "assets.json", mode=0o600, maximum=_MAXIMUM), "cloud asset catalog")
        if set(data) != {"schema", "project_id", "assets", "names", "consumers", "stores"}:
            _fail("catalog fields are invalid")
        if data["schema"] != SCHEMA or data["project_id"] != self.project_id:
            _fail("catalog project or schema mismatch")
        for field in ("assets", "names", "consumers", "stores"):
            if not isinstance(data[field], dict):
                _fail("catalog map is invalid")
        for identity, record in data["assets"].items():
            if not isinstance(record, dict) or set(record) != {"descriptor", "blobs"}:
                _fail("asset record is invalid")
            descriptor = record["descriptor"]
            if _identity(descriptor) != identity or descriptor.get("project_id") != self.project_id:
                _fail("asset identity changed")
            layers = [{k: v for k, v in node.items() if k in _LAYER_KEYS} for node in descriptor.get("layers", [])]
            if _asset(self.project_id, descriptor.get("arch"), descriptor.get("source"), layers) != descriptor:
                _fail("asset graph is invalid")
            if not isinstance(record["blobs"], dict) or set(record["blobs"]) != {
                n["digest"] for n in descriptor["layers"]
            }:
                _fail("asset payload bindings are incomplete")
            for binding in record["blobs"].values():
                if (
                    not isinstance(binding, dict)
                    or set(binding) != {"path"}
                    or not isinstance(binding["path"], str)
                    or not Path(binding["path"]).is_absolute()
                ):
                    _fail("asset payload path binding is malformed")
        if any(value not in data["assets"] for value in data["names"].values()):
            _fail("name references an absent asset")
        for volume_id, store in data["stores"].items():
            if not isinstance(store, dict) or set(store) != {"asset_ids", "volume_id", "filesystem_uuid", "mode"}:
                _fail("sealed store record is malformed")
            if (
                store["volume_id"] != volume_id
                or store["mode"] != "ro"
                or not isinstance(store["asset_ids"], list)
                or not store["asset_ids"]
            ):
                _fail("sealed store identity is malformed")
            if any(asset_id not in data["assets"] for asset_id in store["asset_ids"]):
                _fail("sealed store references an absent asset")
        for consumer, binding in data["consumers"].items():
            _identifier(consumer, "consumer")
            if not isinstance(binding, dict) or set(binding) != {"asset_id", "volume_id"}:
                _fail("consumer binding is malformed")
            store = data["stores"].get(binding["volume_id"])
            if store is None or binding["asset_id"] not in store["asset_ids"]:
                _fail("consumer references an absent asset/store binding")
        return data

    def _save(self, fd: int, data: dict) -> None:
        payload = _canonical(data)
        if len(payload) > _MAXIMUM:
            _fail("catalog exceeds its size bound")
        temporary = ".assets-" + uuid.uuid4().hex
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, "assets.json", src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass

    def register(
        self, *, arch: str, source: dict, layers: list[dict], blob_paths: list[Path], name: str | None = None
    ) -> dict:
        """Register only fully verified successful outputs; no metadata-only success."""
        descriptor = _asset(self.project_id, arch, source, layers)
        if len(blob_paths) != len(layers):
            _fail("one payload path is required for every ordered occurrence")
        if name is not None:
            _identifier(name, "asset name")
        with self._locked() as fd:
            catalog = self._load(fd)
            identity = _identity(descriptor)
            if name in catalog["names"] and catalog["names"][name] != identity:
                _fail("asset name is already bound to different immutable content")
            bindings = {}
            for node, path in zip(descriptor["layers"], blob_paths, strict=True):
                verified = verify_blob(
                    path, node["digest"], node["size"], normalized_oci=source["kind"] != "nova-build"
                )
                # Kernel device/inode IDs are operation-local evidence, not a
                # capability that may be transferred to another worker host.
                binding = {"path": verified["path"]}
                if node["digest"] in bindings and bindings[node["digest"]] != binding:
                    _fail("one output digest has inconsistent physical bindings")
                bindings[node["digest"]] = binding
            record = {"descriptor": descriptor, "blobs": bindings}
            if identity in catalog["assets"] and catalog["assets"][identity] != record:
                _fail("retained asset payload paths changed; explicit new storage occurrence required")
            catalog["assets"][identity] = record
            if name is not None:
                catalog["names"][name] = identity
            self._save(fd, catalog)
            return {"asset_id": identity, **record, "retention": "permanent"}

    def inspect(self, value: str | None = None, *, verify: bool = False) -> dict:
        with self._locked() as fd:
            catalog = self._load(fd)
            identities = list(catalog["assets"]) if value is None else [catalog["names"].get(value, value)]
            result = {}
            for identity in identities:
                if identity not in catalog["assets"]:
                    _fail("asset is absent")
                record = catalog["assets"][identity]
                if verify:
                    for node in record["descriptor"]["layers"]:
                        previous = record["blobs"][node["digest"]]
                        verify_blob(
                            Path(previous["path"]),
                            node["digest"],
                            node["size"],
                            normalized_oci=record["descriptor"]["source"]["kind"] != "nova-build",
                        )
                result[identity] = record
            return {
                "schema": SCHEMA,
                "project_id": self.project_id,
                "retention": "permanent",
                "assets": result,
                "names": catalog["names"],
                "consumers": catalog["consumers"],
                "stores": catalog["stores"],
                "verified": verify,
            }

    def bind_store(self, asset_id: str, *, volume_id: str, filesystem_uuid: str) -> dict:
        """Record an already sealed, operator-verified RO multiattach store, not an attachment grant."""
        _digest(asset_id)
        for value in (volume_id, filesystem_uuid):
            try:
                if str(uuid.UUID(value)) != value:
                    _fail("store IDs must be canonical UUIDs")
            except (ValueError, TypeError, AttributeError):
                _fail("store IDs must be canonical UUIDs")
        with self._locked() as fd:
            catalog = self._load(fd)
            if asset_id not in catalog["assets"]:
                _fail("cannot bind storage for an absent asset")
            previous = catalog["stores"].get(volume_id)
            if previous is not None and (previous["filesystem_uuid"] != filesystem_uuid or previous["mode"] != "ro"):
                _fail("retained store filesystem binding cannot be changed")
            asset_ids = set(previous["asset_ids"]) if previous else set()
            asset_ids.add(asset_id)
            binding = {
                "asset_ids": sorted(asset_ids),
                "volume_id": volume_id,
                "filesystem_uuid": filesystem_uuid,
                "mode": "ro",
            }
            catalog["stores"][volume_id] = binding
            self._save(fd, catalog)
            return binding

    def retain_many(self, asset_id: str, consumers: list[str], *, volume_id: str) -> dict:
        """Bind a batch after preacquisition, all-or-nothing and without any payload copies."""
        _digest(asset_id)
        if not isinstance(consumers, list) or not consumers or len(set(consumers)) != len(consumers):
            _fail("consumer batch must contain unique identifiers")
        for consumer in consumers:
            _identifier(consumer, "consumer")
        with self._locked() as fd:
            catalog = self._load(fd)
            store = catalog["stores"].get(volume_id)
            if asset_id not in catalog["assets"] or store is None or asset_id not in store["asset_ids"]:
                _fail("complete asset and sealed RO store are required before allocating consumers")
            wanted = {"asset_id": asset_id, "volume_id": volume_id}
            if any(c in catalog["consumers"] and catalog["consumers"][c] != wanted for c in consumers):
                _fail("consumer is already bound to different immutable content")
            for consumer in consumers:
                catalog["consumers"][consumer] = wanted
            self._save(fd, catalog)
            return {"asset_id": asset_id, "store": store, "consumers": consumers, "retention": "permanent"}

    def release(self, consumer: str) -> dict:
        _identifier(consumer, "consumer")
        with self._locked() as fd:
            catalog = self._load(fd)
            previous = catalog["consumers"].pop(consumer, None)
            if previous is not None:
                self._save(fd, catalog)
            return {
                "consumer": consumer,
                "released": previous is not None,
                "binding": previous,
                "retention": "permanent",
            }
