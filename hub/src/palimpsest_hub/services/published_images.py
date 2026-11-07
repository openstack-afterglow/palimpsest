"""One registration engine for HTTPS local pushes and isolated worker results."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from palimpsest_local.image_manifest import manifest_digest, normalize_manifest, public_manifest
from sqlalchemy import LargeBinary, and_, cast, or_, select

from palimpsest_hub.config import get_settings
from palimpsest_hub.image_models import (
    PalimpsestHubImageMembership,
    PalimpsestHubImageSourceBlob,
    PalimpsestHubLayerNode,
    PalimpsestHubPublicationPayload,
    PalimpsestHubPublishedImage,
)
from palimpsest_hub.models import PalimpsestHubLayer, exact_identity
from palimpsest_hub.services import image_quota
from palimpsest_hub.services.blob_references import retain_blob
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_store import MEDIA_TYPE_LAYER_SQUASHFS, HubStoreError, LocalPathBlobStore
from palimpsest_hub.services.worker_leases import run_blocking

# Physical payload rows keep the Hub's existing arch vocabulary (x86_64/aarch64).
PLATFORM_ARCH = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}
# One SQL mutex serializes every image publication, quota check and policy change.
IMAGE_MUTEX = "build-api:image:single-flight"


def normalized(value):
    try:
        return normalize_manifest(value)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise HTTPException(status_code=422, detail="invalid image manifest") from None


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def visibility_value(value):
    if value not in ("private", "internal", "public"):
        raise HTTPException(status_code=422, detail="invalid image visibility")
    return value


def visible_images(actor):
    """SQL predicate shared by image, graph and physical-payload authorization."""
    project, user = actor.get("project_id"), actor.get("user_id")
    if not project or not user:
        raise HTTPException(status_code=403, detail="authenticated image identity is required")
    return or_(
        PalimpsestHubPublishedImage.visibility == "public",
        and_(
            PalimpsestHubPublishedImage.visibility == "private",
            exact_identity(PalimpsestHubPublishedImage.created_by, user),
        ),
        and_(
            PalimpsestHubPublishedImage.visibility == "internal",
            exact_identity(PalimpsestHubPublishedImage.project_id, project),
        ),
    )


def image_readable(row, actor):
    return bool(
        actor.get("user_id")
        and actor.get("project_id")
        and (
            row.visibility == "public"
            or (row.visibility == "private" and row.created_by == actor["user_id"])
            or (row.visibility == "internal" and row.project_id == actor["project_id"])
        )
    )


def visible_nodes(actor, *, project_id=None, image_id=None):
    membership = (
        select(PalimpsestHubImageMembership.node_id)
        .join(
            PalimpsestHubPublishedImage,
            PalimpsestHubPublishedImage.id == PalimpsestHubImageMembership.image_id,
        )
        .where(
            visible_images(actor),
            cast(PalimpsestHubImageMembership.project_id, LargeBinary)
            == cast(PalimpsestHubLayerNode.project_id, LargeBinary),
            PalimpsestHubImageMembership.node_id == PalimpsestHubLayerNode.id,
        )
    )
    if project_id is not None:
        membership = membership.where(exact_identity(PalimpsestHubImageMembership.project_id, project_id))
    if image_id is not None:
        membership = membership.where(PalimpsestHubImageMembership.image_id == image_id)
    return select(PalimpsestHubLayerNode).where(membership.exists())


def audience_within(ceilings, visibility, project):
    return isinstance(ceilings, list) and all(
        isinstance(origin, str) and origin and visibility == "internal" and project == origin for origin in ceilings
    )


async def image_reuse_policies(session, row, actor, visibility, *, digest=None):
    """Publication authorship never erases restrictions on borrowed ancestors."""
    if not image_readable(row, actor):
        return None
    await image_value(session, row, internal=True)
    memberships = (
        await session.scalars(
            select(PalimpsestHubImageMembership).where(PalimpsestHubImageMembership.image_id == row.id)
        )
    ).all()
    inherited = [] if row.created_by == actor["user_id"] or row.visibility == "public" else [row.project_id]
    policies = {}
    for member in memberships:
        if digest is not None and member.blob_digest != digest:
            continue
        ceilings = sorted(set([*member.reuse_projects_json, *inherited, *policies.get(member.blob_digest, [])]))
        if not audience_within(ceilings, visibility, actor["project_id"]):
            return None
        policies[member.blob_digest] = ceilings
    return policies


async def reusable_payload(session, digest, size, actor, visibility):
    rows = (
        await session.scalars(
            select(PalimpsestHubPublishedImage)
            .join(
                PalimpsestHubImageMembership,
                PalimpsestHubImageMembership.image_id == PalimpsestHubPublishedImage.id,
            )
            .where(
                PalimpsestHubImageMembership.blob_digest == digest,
                visible_images(actor),
            )
        )
    ).all()
    for row in rows:
        policies = await image_reuse_policies(session, row, actor, visibility, digest=digest)
        if policies is not None and any(
            item["digest"] == digest and item["size_bytes"] == size for item in row.manifest_json["layers"]
        ):
            return policies[digest]
    return None


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SOURCE_ROLES = ("root", "manifest", "config", "layer")
SOURCE_CLOSURE_LIMIT = 300
_SOURCE_DOCUMENT_BYTES = 1024 * 1024
_IMAGE_MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


def _is_squashfs(path):
    with open(path, "rb") as stream:
        return stream.read(4) == b"hsqs"


def _incoherent(reason):
    # Reasons describe only the caller's own declaration and own staged documents.
    return HTTPException(status_code=422, detail="image source closure is incoherent: " + reason)


def normalize_source_closure(items, manifest):
    """Shape-check a declared OCI closure; ``None`` means a publication without source."""
    if items is None:
        return None
    if manifest["mode"] != "oci-root":
        raise _incoherent("only oci-root images carry an OCI source closure")
    if not isinstance(items, list) or not 1 <= len(items) <= SOURCE_CLOSURE_LIMIT:
        raise _incoherent(f"between 1 and {SOURCE_CLOSURE_LIMIT} items are required")
    result, sizes = {}, {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"role", "digest", "size_bytes", "ordinal"}:
            raise _incoherent("each item needs exactly role, digest, size_bytes and ordinal")
        role, digest, size, ordinal = item["role"], item["digest"], item["size_bytes"], item["ordinal"]
        if role not in SOURCE_ROLES or not isinstance(digest, str) or not _DIGEST.match(digest):
            raise _incoherent("unknown role or invalid digest")
        if type(size) is not int or not 1 <= size <= image_quota.MAX_BIGINT:
            raise _incoherent("size_bytes must be a positive integer")
        if type(ordinal) is not int or not 0 <= ordinal < SOURCE_CLOSURE_LIMIT:
            raise _incoherent("ordinal must be a small non-negative integer")
        if (role, ordinal) in result:
            raise _incoherent("duplicate role and ordinal")
        if sizes.setdefault(digest, size) != size:
            raise _incoherent("one digest declared with two sizes")
        result[(role, ordinal)] = {"role": role, "digest": digest, "size_bytes": size, "ordinal": ordinal}
    counts = {role: sorted(ordinal for kind, ordinal in result if kind == role) for role in SOURCE_ROLES}
    if counts["manifest"] != [0] or counts["config"] != [0] or counts["root"] not in ([], [0]):
        raise _incoherent("exactly one manifest and config and at most one root, each at ordinal 0")
    if not counts["layer"] or counts["layer"] != list(range(len(counts["layer"]))):
        raise _incoherent("layer ordinals must be contiguous from 0")
    if any(item["digest"] in sizes and item["size_bytes"] != sizes[item["digest"]] for item in manifest["layers"]):
        raise _incoherent("a closure digest contradicts a payload size")
    _require_closure_commitment(manifest, result)
    return [result[key] for key in sorted(result, key=lambda key: (SOURCE_ROLES.index(key[0]), key[1]))]


def _require_closure_commitment(manifest, slots):
    """The hashed manifest must name its closure, so ``manifest_digest`` commits to it.

    The closure is stored outside ``manifest_json``; without these fields the same
    manifest digest could be republished with a different (coherent) OCI closure.
    """
    root = slots.get(("root", 0))
    expected = {
        "manifest_digest": slots[("manifest", 0)]["digest"],
        "config_digest": slots[("config", 0)]["digest"],
        "root_digest": root["digest"] if root is not None else slots[("manifest", 0)]["digest"],
    }
    source = manifest.get("source") or {}
    for key, value in expected.items():
        if source.get(key) != value:
            raise _incoherent(f"image source {key} is missing or differs from the closure")
    covered = set()
    for item in manifest["layers"]:
        origin = item["source"].get("oci_layer") if isinstance(item.get("source"), dict) else None
        compressed = origin.get("compressed") if isinstance(origin, dict) else None
        ordinal = origin.get("ordinal") if isinstance(origin, dict) else None
        slot = slots.get(("layer", ordinal)) if type(ordinal) is int else None
        if slot is None or _descriptor(compressed) != (slot["digest"], slot["size_bytes"]):
            raise _incoherent("every payload must name its original OCI layer, matching the closure at that ordinal")
        covered.add(ordinal)
    if covered != {ordinal for role, ordinal in slots if role == "layer"}:
        raise _incoherent("some closure layers are not the origin of any payload")


def source_bytes(closure):
    """Each distinct closure digest counts once."""
    return sum({item["digest"]: item["size_bytes"] for item in closure or ()}.values())


class _DuplicateKey(ValueError):
    pass


def _pairs_reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate key {key!r}")
        result[key] = value
    return result


def _read_document(store, item, *, schema_version=2):
    """Bounded, re-hashed read of an immutable CAS document blob (blocking)."""
    if item["size_bytes"] > _SOURCE_DOCUMENT_BYTES:
        raise _incoherent(f"{item['role']} document exceeds 1 MiB")
    try:
        with open(store.blob_path(item["digest"]), "rb") as stream:
            raw = stream.read(item["size_bytes"] + 1)
    except OSError:
        raw = b""
    if len(raw) != item["size_bytes"] or "sha256:" + hashlib.sha256(raw).hexdigest() != item["digest"]:
        raise HTTPException(status_code=409, detail="publication payload unavailable or changed")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs_reject_duplicates)
    except _DuplicateKey:
        raise _incoherent(f"{item['role']} document contains duplicate keys") from None
    except (ValueError, UnicodeError, RecursionError):
        raise _incoherent(f"{item['role']} document is not JSON") from None
    if not isinstance(value, dict):
        raise _incoherent(f"{item['role']} document must be a JSON object")
    if schema_version is not None and value.get("schemaVersion") != schema_version:
        raise _incoherent(f"{item['role']} document is not a schemaVersion {schema_version} OCI/Docker document")
    return value


def _descriptor(value):
    if not isinstance(value, dict):
        return None
    digest, size = value.get("digest"), value.get("size")
    return (digest, size) if isinstance(digest, str) and type(size) is int else None


def _check_closure_documents(store, manifest, closure):
    """Every declared byte must be the exact OCI chain the closure declares (blocking)."""
    by_role = {}
    for item in closure:
        by_role.setdefault(item["role"], []).append(item)
    image_manifest, config, layers = by_role["manifest"][0], by_role["config"][0], by_role["layer"]
    document = _read_document(store, image_manifest, schema_version=2)
    media = document.get("mediaType")
    if media is not None and media not in _IMAGE_MANIFEST_TYPES:
        raise _incoherent("manifest document is not an OCI image manifest or Docker v2 schema 2 manifest")
    declared = document.get("layers")
    if _descriptor(document.get("config")) != (config["digest"], config["size_bytes"]):
        raise _incoherent("manifest config descriptor differs from the config item")
    if not isinstance(declared, list) or [_descriptor(item) for item in declared] != [
        (item["digest"], item["size_bytes"]) for item in layers
    ]:
        raise _incoherent("manifest layer descriptors differ from the layer items (digest, size, order or count)")
    config_doc = _read_document(store, config, schema_version=None)
    source_diff_ids = (manifest.get("source") or {}).get("diff_ids")
    if source_diff_ids is not None:
        rootfs = config_doc.get("rootfs") if isinstance(config_doc, dict) else None
        config_diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
        if config_diff_ids != source_diff_ids:
            raise _incoherent("config rootfs.diff_ids differs from manifest source.diff_ids")
    root = by_role.get("root", [None])[0]
    if root is not None:
        index = _read_document(store, root, schema_version=2)
        media = index.get("mediaType")
        entries = index.get("manifests")
        if (media is not None and media not in _INDEX_TYPES) or not isinstance(entries, list):
            raise _incoherent("root document is not an OCI index or Docker manifest list")
        if (image_manifest["digest"], image_manifest["size_bytes"]) not in {_descriptor(item) for item in entries}:
            raise _incoherent("root document does not list the manifest")


async def verify_source_closure(session, store, actor, manifest, closure):
    """Own staging proves possession: identical bytes elsewhere never grant access."""
    for digest, size in {item["digest"]: item["size_bytes"] for item in closure}.items():
        stage = await session.get(
            PalimpsestHubPublicationPayload,
            {"project_id": actor["project_id"], "created_by": actor["user_id"], "blob_digest": digest},
        )
        if stage is None or stage.project_id != actor["project_id"] or stage.created_by != actor["user_id"]:
            raise HTTPException(status_code=404, detail="publication payload not found")
        if stage.size_bytes != size:
            # The caller's own staged size; revealing the mismatch discloses nothing foreign.
            raise _incoherent("a declared size differs from the staged blob")
    await run_blocking(_check_closure_documents, store, manifest, closure)


async def stored_source_closure(session, image_id):
    rows = (
        await session.scalars(
            select(PalimpsestHubImageSourceBlob).where(PalimpsestHubImageSourceBlob.image_id == image_id)
        )
    ).all()
    value = [
        {"role": row.role, "digest": row.blob_digest, "size_bytes": row.size_bytes, "ordinal": row.ordinal}
        for row in rows
    ]
    if any(item["role"] not in SOURCE_ROLES for item in value):
        raise HTTPException(status_code=409, detail="published image source closure is invalid")
    return sorted(value, key=lambda item: (SOURCE_ROLES.index(item["role"]), item["ordinal"]))


async def image_blob_roles(session, row, digest):
    """Roles under which ``digest`` belongs to this image, and its recorded size.

    Returns ``(roles, size_bytes)``: ``"payload"`` for SquashFS layer payloads,
    ``"source-<role>"`` for OCI source-closure blobs; ``([], None)`` for non-members.
    """
    roles, size = [], None
    for item in normalized(row.manifest_json)["layers"]:
        if item["digest"] == digest:
            roles, size = ["payload"], item["size_bytes"]
            break
    for item in await stored_source_closure(session, row.id):
        if item["digest"] == digest:
            if size is not None and size != item["size_bytes"]:
                raise HTTPException(status_code=409, detail="published image source closure is invalid")
            roles.append("source-" + item["role"])
            size = item["size_bytes"]
    return sorted(set(roles)), size


def _open_directory(path):
    """Walk every component without following symlinks, retaining the actual root."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("publication root must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _fd_identity(fd):
    metadata = os.fstat(fd)
    values = [
        line.split(":", 1)[1].strip()
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
        if line.startswith("mnt_id:")
    ]
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError("publication descriptor lacks mount identity")
    return int(values[0]), metadata.st_dev, metadata.st_ino


def _mount_identity(root, fd, export, exported_path):
    identity = _fd_identity(fd)
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        parts, filesystem = left.split(), right.split()
        mount = Path(_unescape_mount(parts[4]))
        if root in mount.parents:
            raise ValueError("publication filesystem has a child mount")
        if root == mount:
            mounts.append(
                (int(parts[0]), parts[2], _unescape_mount(parts[3]), filesystem[0], _unescape_mount(filesystem[1]))
            )
    expected_device = f"{os.major(identity[1])}:{os.minor(identity[1])}"
    if len(mounts) != 1 or mounts[0][:2] != (identity[0], expected_device):
        raise ValueError("publication mount does not own the open root")
    _, _, mount_root, filesystem, source = mounts[0]
    if mount_root not in ("/", exported_path) or filesystem != "ceph" or source != export:
        raise ValueError("publication export differs")
    return identity


class _PinnedManilaStore(LocalPathBlobStore):
    def __init__(self, root, fd, export, exported_path):
        super().__init__(root)
        self.fd, self.export, self.exported_path = fd, export, exported_path
        self.target_fds = []
        self.identity = _mount_identity(root, fd, export, exported_path)

    def close(self):
        for fd in reversed(self.target_fds):
            os.close(fd)
        os.close(self.fd)

    def pin_target(self, directories):
        # Keep the first qualified target namespace alive across every layer and
        # through the final registration check, not merely during each copy.
        if not self.target_fds:
            for fd in directories[1:]:
                self.target_fds.append(os.dup(fd))

    def check_identity(self):
        try:
            if _mount_identity(self.root, self.fd, self.export, self.exported_path) != self.identity:
                raise HubStoreError("publication root identity changed")
            current = _open_directory(self.root)
            try:
                if _fd_identity(current) != self.identity:
                    raise HubStoreError("publication root path changed")
            finally:
                os.close(current)
            directories = [self.fd, *self.target_fds]
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            for parent, child, component in zip(directories, self.target_fds, ("blobs", "sha256"), strict=False):
                current = os.open(component, flags, dir_fd=parent)
                try:
                    if _fd_identity(current) != _fd_identity(child):
                        raise HubStoreError("publication target namespace changed")
                finally:
                    os.close(current)
        except (OSError, ValueError, IndexError):
            raise HubStoreError("publication mount identity is unavailable or changed") from None


def storage_destination(store):
    """Only a kernel-observed CephFS export can be called a Manila binding."""
    settings = get_settings()
    fields = [
        getattr(settings, "palimpsest_hub_publication_manila_" + key, "") for key in ("share_id", "export", "root")
    ]
    if not any(fields):
        return store, {"kind": "local", "root": "blobs/sha256"}
    share, export, root_value = fields
    try:
        from palimpsest_local.cloud_env import _ceph_source_identity

        exported_path = _ceph_source_identity(export)[1]
        if not all(fields) or str(UUID(share)) != share:
            raise ValueError
        root = Path(root_value)
        fd = None
        if not root.is_absolute():
            raise ValueError
        staging_root = store.root.resolve()
        if staging_root == root or root in staging_root.parents:
            raise ValueError
        fd = _open_directory(root)
        destination = _PinnedManilaStore(root, fd, export, exported_path)
        destination.check_identity()
    except (OSError, ValueError, IndexError, HubStoreError):
        if "fd" in locals() and fd is not None:
            os.close(fd)
        raise HTTPException(status_code=503, detail="configured Manila publication mount is not verified") from None
    return destination, {"kind": "manila", "share_id": share, "export": export, "root": "blobs/sha256"}


def _publish_pinned(source, destination, verified):
    """All namespace mutations are relative to qualified, pinned directories."""
    destination.check_identity()
    directories = [destination.fd]
    temporary = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        for component in ("blobs", "sha256"):
            with suppress(FileExistsError):
                os.mkdir(component, dir_fd=directories[-1])
            child = os.open(component, flags, dir_fd=directories[-1])
            directories.append(child)
            if _fd_identity(child)[:2] != destination.identity[:2]:
                raise HubStoreError("publication child filesystem changed")
        destination.pin_target(directories)

        def check_target_directory():
            destination.check_identity()
            for parent, child, component in zip(directories, directories[1:], ("blobs", "sha256"), strict=False):
                current = os.open(component, flags, dir_fd=parent)
                try:
                    if _fd_identity(current) != _fd_identity(child):
                        raise HubStoreError("publication target directory changed")
                finally:
                    os.close(current)

        def inspect_existing():
            try:
                fd = os.open(
                    verified.blob_digest[7:], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directories[-1]
                )
            except FileNotFoundError:
                return False
            with os.fdopen(fd, "rb") as incoming:
                if not stat.S_ISREG(os.fstat(fd).st_mode) or _fd_identity(fd)[:2] != destination.identity[:2]:
                    raise HubStoreError("publication target filesystem changed")
                if hasattr(destination, "check_payload_layout"):
                    destination.check_payload_layout(fd)
                sha, size = hashlib.sha256(), 0
                while chunk := incoming.read(1024 * 1024):
                    size += len(chunk)
                    if size > verified.size_bytes:
                        raise HubStoreError("immutable publication payload conflicts")
                    sha.update(chunk)
                if size != verified.size_bytes or "sha256:" + sha.hexdigest() != verified.blob_digest:
                    raise HubStoreError("immutable publication payload conflicts")
                os.fsync(fd)
            return True

        check_target_directory()
        if not inspect_existing():
            name = ".publication-" + uuid4().hex
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directories[-1])
            temporary = name
            with (
                os.fdopen(fd, "wb") as outgoing,
                os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as incoming,
            ):
                if hasattr(destination, "check_payload_layout"):
                    destination.check_payload_layout(outgoing.fileno())
                if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                    raise HubStoreError("publication input must be regular")
                sha, size = hashlib.sha256(), 0
                while chunk := incoming.read(1024 * 1024):
                    size += len(chunk)
                    if size > verified.size_bytes:
                        raise HubStoreError("publication input changed")
                    sha.update(chunk)
                    outgoing.write(chunk)
                if size != verified.size_bytes or "sha256:" + sha.hexdigest() != verified.blob_digest:
                    raise HubStoreError("publication input changed")
                outgoing.flush()
                if hasattr(destination, "check_payload_layout"):
                    destination.check_payload_layout(outgoing.fileno())
                os.fchmod(outgoing.fileno(), 0o444)
                os.fsync(outgoing.fileno())
            check_target_directory()
            try:
                os.link(
                    temporary,
                    verified.blob_digest[7:],
                    src_dir_fd=directories[-1],
                    dst_dir_fd=directories[-1],
                    follow_symlinks=False,
                )
            except FileExistsError:
                inspect_existing()
        for fd in reversed(directories):
            os.fsync(fd)
        check_target_directory()
    finally:
        try:
            if temporary is not None:
                os.unlink(temporary, dir_fd=directories[-1])
        finally:
            for fd in reversed(directories[1:]):
                os.close(fd)


def _publish_no_replace(source, destination, verified):
    """Publish durable immutable bytes; never repair/overwrite a published path."""
    if hasattr(destination, "fd"):
        return _publish_pinned(source, destination, verified)
    target = destination.blob_path(verified.blob_digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.resolve() != target.parent or target.parent.is_symlink():
        raise HubStoreError("publication target contains symlinks")
    if target.exists() or target.is_symlink():
        if destination.inspect_file(target, max_bytes=verified.size_bytes) != verified:
            raise HubStoreError("immutable publication payload conflicts")
        destination._sync_existing_blob(target)
        return
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".publication-")
    try:
        sha, size = hashlib.sha256(), 0
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(fd, "wb") as outgoing, os.fdopen(os.open(source, flags), "rb") as incoming:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise HubStoreError("publication input must be regular")
            while chunk := incoming.read(1024 * 1024):
                size += len(chunk)
                if size > verified.size_bytes:
                    raise HubStoreError("publication input changed")
                sha.update(chunk)
                outgoing.write(chunk)
            if size != verified.size_bytes or "sha256:" + sha.hexdigest() != verified.blob_digest:
                raise HubStoreError("publication input changed")
            outgoing.flush()
            os.fchmod(outgoing.fileno(), 0o444)
            os.fsync(outgoing.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if destination.inspect_file(target, max_bytes=size) != verified:
                raise HubStoreError("immutable publication payload conflicts") from None
        destination._sync_blob_dir()
    finally:
        os.unlink(temporary)


def serialize_node(row):
    return {
        "id": row.id,
        "project_id": row.project_id,
        "parent_id": row.parent_id,
        "digest": row.blob_digest,
        "size_bytes": row.size_bytes,
        "payload_path": "blobs/sha256/" + row.blob_digest[7:],
        **row.compatibility_json,
    }


async def image_value(session, row, *, internal=False, actor=None):
    manifest = normalized(row.manifest_json)
    if manifest_digest(manifest) != row.manifest_digest or manifest["layers"][-1]["id"] != row.leaf_id:
        raise HTTPException(status_code=409, detail="published image identity is inconsistent")
    memberships = (
        await session.scalars(
            select(PalimpsestHubImageMembership)
            .where(PalimpsestHubImageMembership.image_id == row.id)
            .order_by(PalimpsestHubImageMembership.ordinal)
        )
    ).all()
    if [(item.ordinal, item.project_id, item.node_id, item.blob_digest) for item in memberships] != [
        (ordinal, row.project_id, item["id"], item["digest"]) for ordinal, item in enumerate(manifest["layers"])
    ]:
        raise HTTPException(status_code=409, detail="published image membership is incomplete")
    if any(not audience_within(item.reuse_projects_json, row.visibility, row.project_id) for item in memberships):
        raise HTTPException(status_code=409, detail="published image audience exceeds inherited restrictions")
    nodes = []
    for item in manifest["layers"]:
        node = await session.get(PalimpsestHubLayerNode, (row.project_id, item["id"]))
        if (
            node is None
            or node.project_id != row.project_id
            or node.blob_digest != item["digest"]
            or node.parent_id != item["parent_id"]
            or node.size_bytes != item["size_bytes"]
        ):
            raise HTTPException(status_code=409, detail="published image graph is incomplete")
        nodes.append(serialize_node(node))
    return {
        "id": row.id,
        "name": row.name,
        "manifest_digest": row.manifest_digest,
        "project_id": row.project_id,
        "visibility": row.visibility,
        "manifest": manifest if internal else public_manifest(manifest),
        "leaf_id": row.leaf_id,
        "storage_binding": row.storage_binding_json,
        "nodes": nodes,
        "source_closure": await stored_source_closure(session, row.id),
        "created_at": row.created_at.isoformat(),
        **({"created_by": row.created_by} if internal or (actor and actor.get("user_id") == row.created_by) else {}),
    }


async def resolve_image(factory, image_id, actor):
    """Exact authorized server manifest, including personal/public cross-project use."""
    async with factory() as session:
        row = await session.get(PalimpsestHubPublishedImage, image_id)
        if row is None or not image_readable(row, actor):
            raise HTTPException(status_code=404, detail="published image not found")
        return await image_value(session, row, internal=True)


async def register_image(
    session,
    store,
    actor,
    request_id,
    name,
    manifest,
    *,
    visibility="private",
    require_staged=False,
    image_id=None,
    inherited_policies=None,
    source_closure=None,
):
    """Caller holds all payload locks and owns the encompassing SQL transaction."""
    manifest = normalized(manifest)
    visibility = visibility_value(visibility)
    visible_images(actor)
    identity = manifest_digest(manifest)
    closure = normalize_source_closure(source_closure, manifest)
    project = actor["project_id"]
    existing = await session.scalar(
        select(PalimpsestHubPublishedImage)
        .where(
            exact_identity(PalimpsestHubPublishedImage.project_id, project),
            PalimpsestHubPublishedImage.request_id == request_id,
        )
        .with_for_update()
    )
    if existing is not None:
        if (
            existing.created_by != actor["user_id"]
            or existing.visibility != visibility
            or existing.manifest_digest != identity
            or existing.name != name
            or (image_id is not None and existing.id != image_id)
        ):
            raise HTTPException(status_code=409, detail="publication request_id conflicts")
        if await stored_source_closure(session, existing.id) != (closure or []):
            raise HTTPException(status_code=409, detail="publication request_id conflicts")
        return existing
    # Replay above returns before quota; the caller's global image mutex makes check-then-insert atomic.
    await image_quota.enforce(session, actor, name, manifest, visibility, source=source_bytes(closure))
    if closure:
        await verify_source_closure(session, store, actor, manifest, closure)
    opened = []

    def open_destination():
        destination, binding = storage_destination(store)
        opened.append(destination)
        if binding["kind"] == "manila":
            from palimpsest_hub.services.ceph_scoped_store import scoped_destination

            settings = get_settings()
            destination = scoped_destination(
                (destination, binding),
                actor,
                visibility,
                fs_name=getattr(settings, "palimpsest_hub_publication_manila_fs_name", ""),
                data_pool=getattr(settings, "palimpsest_hub_publication_manila_data_pool", ""),
            )
            opened[:] = [destination]
            binding = destination.binding
        return destination, binding

    try:
        try:
            destination, binding = await run_blocking(open_destination)
        except (HubStoreError, OSError, ValueError):
            raise HTTPException(status_code=503, detail="publication namespace isolation is not verified") from None
        return await _register_payloads(
            session,
            store,
            actor,
            request_id,
            name,
            manifest,
            destination,
            binding,
            visibility=visibility,
            require_staged=require_staged,
            image_id=image_id,
            inherited_policies=inherited_policies or {},
            closure=closure,
        )
    finally:
        for destination in opened:
            if hasattr(destination, "close"):
                destination.close()


async def _register_payloads(
    session,
    store,
    actor,
    request_id,
    name,
    manifest,
    destination,
    binding,
    *,
    visibility,
    require_staged,
    image_id,
    inherited_policies,
    closure,
):
    from palimpsest_hub.api.hub import _visible_filter

    project, identity = actor["project_id"], manifest_digest(manifest)
    compatibility = {key: manifest[key] for key in ("mode", "platform", "os_family", "os_version", "base_image_digest")}
    arch = PLATFORM_ARCH.get(manifest["platform"])
    if arch is None:
        raise HTTPException(status_code=422, detail="unsupported image platform")
    reuse_policies = {}
    for item in {item["digest"]: item for item in manifest["layers"]}.values():
        digest, size = item["digest"], item["size_bytes"]
        reuse_policies[digest] = []
        if require_staged:
            stage = await session.get(
                PalimpsestHubPublicationPayload,
                {"project_id": project, "created_by": actor["user_id"], "blob_digest": digest},
            )
            if stage is None:
                reusable = await reusable_payload(session, digest, size, actor, visibility)
                accessible = await session.scalar(
                    _visible_filter(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest), actor)
                )
                legacy_reusable = (
                    accessible is not None
                    and accessible.size_bytes == size
                    and (
                        accessible.is_published
                        or accessible.project_id is None
                        or accessible.created_by == actor["user_id"]
                        or (visibility == "internal" and accessible.project_id == project)
                    )
                )
                if reusable is None and not legacy_reusable:
                    raise HTTPException(status_code=404, detail="publication payload not found")
                if reusable is not None:
                    reuse_policies[digest] = reusable
                elif (
                    accessible.created_by != actor["user_id"]
                    and not accessible.is_published
                    and accessible.project_id is not None
                ):
                    reuse_policies[digest] = [accessible.project_id]
            elif stage.project_id != project or stage.created_by != actor["user_id"] or stage.size_bytes != size:
                raise HTTPException(status_code=409, detail="publication payload size conflicts")
        reuse_policies[digest] = sorted(set([*reuse_policies[digest], *inherited_policies.get(digest, [])]))
        if not audience_within(reuse_policies[digest], visibility, project):
            raise HTTPException(status_code=404, detail="publication payload not found")
        try:
            verified = await run_blocking(store.inspect_file, store.blob_path(digest), max_bytes=size)
            if verified.blob_digest != digest or verified.size_bytes != size:
                raise HubStoreError("payload mismatch")
            squashfs = await run_blocking(_is_squashfs, store.blob_path(digest))
        except (HubStoreError, OSError):
            raise HTTPException(status_code=409, detail="publication payload unavailable or changed") from None
        if not squashfs:
            # Staging accepts opaque source blobs; only SquashFS bytes may become a layer payload.
            raise HTTPException(status_code=422, detail="publication payload is not SquashFS")
        try:
            await run_blocking(_publish_no_replace, store.blob_path(digest), destination, verified)
        except (HubStoreError, OSError):
            raise HTTPException(status_code=409, detail="publication payload unavailable or changed") from None
        physical = await session.scalar(
            select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest).with_for_update()
        )
        if physical is None:
            config = {
                "name": "payload-" + digest[7:63],
                "kind": "image-payload",
                "blob_digest": digest,
                "arch": arch,
                "parent_digest": None,
                "chain_id": None,
                "base_image_digest": None,
                "instance_mode": "oci-layer",
            }
            physical = PalimpsestHubLayer(
                blob_digest=digest,
                blob_md5=verified.blob_md5,
                size_bytes=size,
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                arch=arch,
                name=config["name"],
                kind="image-payload",
                config_digest=compute_config_digest(config),
                config_json=config,
                project_id=project,
                is_published=False,
                created_by=actor.get("user_id"),
            )
            session.add(physical)
        elif (
            physical.size_bytes != size
            or physical.kind not in ("squashfs", "image-payload")
            or physical.media_type != MEDIA_TYPE_LAYER_SQUASHFS
        ):
            # The arch of the first registrant is not a property of the bytes: rejecting
            # another platform here would block it and reveal a private publication.
            raise HTTPException(status_code=409, detail="payload registration conflicts")
        await session.flush()
    # Source closure blobs take the same visibility-scoped storage path as payloads.
    for digest, size in {item["digest"]: item["size_bytes"] for item in closure or ()}.items():
        if digest in reuse_policies:
            continue
        try:
            verified = await run_blocking(store.inspect_file, store.blob_path(digest), max_bytes=size)
            if verified.blob_digest != digest or verified.size_bytes != size:
                raise HubStoreError("payload mismatch")
            await run_blocking(_publish_no_replace, store.blob_path(digest), destination, verified)
        except (HubStoreError, OSError):
            raise HTTPException(status_code=409, detail="publication payload unavailable or changed") from None
    for item in manifest["layers"]:
        node = await session.get(PalimpsestHubLayerNode, (project, item["id"]))
        if node is None:
            session.add(
                PalimpsestHubLayerNode(
                    project_id=project,
                    id=item["id"],
                    parent_id=item["parent_id"],
                    blob_digest=item["digest"],
                    size_bytes=item["size_bytes"],
                    compatibility_json=compatibility,
                )
            )
        elif node.project_id != project or (
            node.parent_id,
            node.blob_digest,
            node.size_bytes,
            node.compatibility_json,
        ) != (item["parent_id"], item["digest"], item["size_bytes"], compatibility):
            raise HTTPException(status_code=409, detail="logical layer registration conflicts")
        await session.flush()
    row = PalimpsestHubPublishedImage(
        id=image_id or str(uuid4()),
        project_id=project,
        created_by=actor.get("user_id"),
        request_id=request_id,
        name=name,
        visibility=visibility,
        manifest_digest=identity,
        manifest_json=manifest,
        leaf_id=manifest["layers"][-1]["id"],
        storage_binding_json=binding,
    )
    session.add(row)
    await session.flush()
    for ordinal, item in enumerate(manifest["layers"]):
        session.add(
            PalimpsestHubImageMembership(
                image_id=row.id,
                ordinal=ordinal,
                project_id=project,
                node_id=item["id"],
                blob_digest=item["digest"],
                reuse_projects_json=reuse_policies[item["digest"]],
            )
        )
    for item in closure or ():
        session.add(
            PalimpsestHubImageSourceBlob(
                image_id=row.id,
                role=item["role"],
                ordinal=item["ordinal"],
                project_id=project,
                blob_digest=item["digest"],
                size_bytes=item["size_bytes"],
            )
        )
    stored = {item["digest"] for item in [*manifest["layers"], *(closure or ())]}
    for digest in stored:
        await retain_blob(session, digest, "published-image", row.id)
    await session.flush()
    if hasattr(destination, "fd"):
        try:
            if hasattr(destination, "check_payload_layout"):
                for digest in stored:
                    await run_blocking(destination.check_payload_layout, digest)
            await run_blocking(destination.check_identity)
        except HubStoreError:
            raise HTTPException(status_code=409, detail="publication storage binding changed") from None
    return row


async def publish_image(
    factory,
    store,
    actor,
    request_id,
    name,
    manifest,
    storage_binding=None,
    *,
    visibility="private",
    base_image_id=None,
    base_manifest_digest=None,
    source_closure=None,
    recheck=None,
):
    from palimpsest_hub.api.hub import _locked_blobs
    from palimpsest_hub.services.worker_leases import sql_mutex

    if storage_binding is not None:
        raise HTTPException(status_code=422, detail="publication storage is operator bound")
    manifest = normalized(manifest)
    closure = normalize_source_closure(source_closure, manifest)
    async with (
        _locked_blobs(store, [item["digest"] for item in [*manifest["layers"], *(closure or ())]]),
        sql_mutex(factory, IMAGE_MUTEX) as session,
    ):
        inherited_policies = {}
        if base_image_id is not None:
            base = await session.get(PalimpsestHubPublishedImage, base_image_id)
            inherited_policies = (
                await image_reuse_policies(session, base, actor, visibility) if base is not None else None
            )
            if inherited_policies is None:
                raise HTTPException(status_code=404, detail="published base image not found")
            if base.manifest_digest != base_manifest_digest:
                raise HTTPException(status_code=409, detail="published base image changed")
            await image_value(session, base, internal=True)
        row = await register_image(
            session,
            store,
            actor,
            request_id,
            name,
            manifest,
            visibility=visibility,
            require_staged=True,
            inherited_policies=inherited_policies,
            source_closure=closure,
        )
        if recheck is not None:
            # Revalidate the original, pre-target-rewrite subject after locks and payload copies.
            await recheck()
        await session.commit()
        return await image_value(session, row, actor=actor)


async def extend_image(
    factory,
    store,
    actor,
    request_id,
    name,
    base_image_id,
    layers,
    *,
    visibility="private",
    metadata=None,
    recheck=None,
):
    base = await resolve_image(factory, base_image_id, actor)
    full = base["manifest"]
    manifest = normalized(
        {
            **full,
            "layers": [
                *full["layers"],
                *[{key: layer[key] for key in ("digest", "size_bytes", "source") if key in layer} for layer in layers],
            ],
            "source": {
                "kind": "local",
                "metadata": {
                    "base_image_id": base_image_id,
                    "base_manifest_digest": base["manifest_digest"],
                },
            },
            "metadata": metadata if metadata is not None else {},
        }
    )
    if sum(item["size_bytes"] for item in manifest["layers"]) > get_settings().palimpsest_hub_max_blob_bytes:
        raise HTTPException(status_code=413, detail="image stack exceeds byte limit")
    return await publish_image(
        factory,
        store,
        actor,
        request_id,
        name,
        manifest,
        visibility=visibility,
        base_image_id=base_image_id,
        base_manifest_digest=base["manifest_digest"],
        recheck=recheck,
    )
