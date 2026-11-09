"""Build the OCI manifest and config of a committed image from its parent and one new layer.

Pure functions over parsed OCI JSON: the new image keeps the parent's layer order and
runtime configuration, appends exactly one layer, and records the parent link.
"""

from __future__ import annotations

import copy
import ctypes
import errno
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import ArtifactValidationError

OCI_COMMIT_ID = "palimpsest.oci-commit.v1"
PARENT_ANNOTATION = "io.palimpsest.commit.parent-manifest"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_CONFIG = "application/vnd.oci.image.config.v1+json"


class CommitError(ArtifactValidationError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class GzipLayer:
    """Identity of a streamed layer. ``diff_id`` (uncompressed) is the stable identity;
    ``digest``/``size`` describe the compressed bytes actually written, which may differ
    between zlib builds for the same tar."""

    digest: str
    size: int
    diff_id: str


@dataclass(frozen=True, slots=True)
class CommittedImage:
    config: bytes
    config_digest: str
    manifest: bytes
    manifest_digest: str


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class GzipTee:
    """Binary sink: tar bytes in, gzip bytes out to ``target``, one pass, O(1) memory.

    Computes the uncompressed digest (DiffID) and the compressed digest/size while writing.
    """

    def __init__(self, target: BinaryIO) -> None:
        self._diff = hashlib.sha256()
        self._out = hashlib.sha256()
        self._size = 0
        self._target = target
        self._gzip = gzip.GzipFile(fileobj=_Counting(self), mode="wb", filename="", mtime=0, compresslevel=6)
        self._closed = False

    def _emit(self, chunk: bytes) -> int:
        view = memoryview(chunk)
        total = 0
        while total < len(chunk):
            n = self._target.write(view[total:])
            if n is None or n <= 0:
                raise OSError("short or failed write to target")
            total += n
        self._out.update(chunk)
        self._size += len(chunk)
        return len(chunk)

    def write(self, data: bytes) -> int:
        if self._closed:
            raise CommitError("commit-layer", "layer sink is closed")
        self._diff.update(data)
        self._gzip.write(data)
        return len(data)

    def flush(self) -> None:
        return None

    def finish(self) -> GzipLayer:
        if not self._closed:
            self._gzip.close()
            self._closed = True
            self._target.flush()
        return GzipLayer(
            digest="sha256:" + self._out.hexdigest(), size=self._size, diff_id="sha256:" + self._diff.hexdigest()
        )


class _Counting:
    def __init__(self, owner: GzipTee) -> None:
        self._owner = owner

    def write(self, chunk: bytes) -> int:
        return self._owner._emit(chunk)

    def flush(self) -> None:
        return None


def gzip_layer(tar_bytes: bytes) -> tuple[bytes, GzipLayer]:
    """Convenience for small inputs/tests; real captures stream through ``GzipTee``."""
    buffer = io.BytesIO()
    tee = GzipTee(buffer)
    tee.write(tar_bytes)
    layer = tee.finish()
    return buffer.getvalue(), layer


def build_committed_image(
    parent_manifest: dict,
    parent_config: dict,
    layer: GzipLayer,
    *,
    created: str,
    comment: str = "palimpsest commit",
    parent_manifest_digest: str | None = None,
) -> CommittedImage:
    if not isinstance(parent_manifest, dict) or not isinstance(parent_config, dict):
        raise CommitError("commit-parent", "parent manifest and config must be objects")
    if not isinstance(parent_manifest_digest, str) or not _DIGEST.fullmatch(parent_manifest_digest):
        raise CommitError("commit-parent", "verified parent manifest digest is required")

    layers = parent_manifest.get("layers")
    rootfs = parent_config.get("rootfs")
    if not isinstance(layers, list) or not layers:
        raise CommitError("commit-parent", "parent manifest has no layers")

    for item in layers:
        if not isinstance(item, dict):
            raise CommitError("commit-parent", "parent layer descriptor must be an object")
        digest = item.get("digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise CommitError("commit-parent", f"invalid parent layer digest: {digest}")
        size = item.get("size")
        if not isinstance(size, int) or size < 0:
            raise CommitError("commit-parent", f"invalid parent layer size: {size}")

    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers" or not isinstance(rootfs.get("diff_ids"), list):
        raise CommitError("commit-parent", "parent config rootfs is invalid")
    if len(rootfs["diff_ids"]) != len(layers):
        raise CommitError("commit-parent", "parent diff_ids do not match its layers")
    for diff_id in rootfs["diff_ids"]:
        if not isinstance(diff_id, str) or not _DIGEST.fullmatch(diff_id):
            raise CommitError("commit-parent", f"invalid parent diff_id: {diff_id}")

    for value in (layer.digest, layer.diff_id):
        if not _DIGEST.fullmatch(value):
            raise CommitError("commit-layer", "layer digest is invalid")
    if not isinstance(layer.size, int) or layer.size < 0:
        raise CommitError("commit-layer", "layer size is invalid")
    if layer.diff_id in rootfs["diff_ids"] or any(item.get("digest") == layer.digest for item in layers):
        raise CommitError("commit-layer", "layer is already part of the parent chain")
    if not isinstance(created, str) or not created:
        raise CommitError("commit-created", "created timestamp is required")

    config = copy.deepcopy(parent_config)
    config["rootfs"]["diff_ids"] = [*rootfs["diff_ids"], layer.diff_id]
    config["created"] = created
    history = list(config.get("history") or [])
    history.append({"created": created, "created_by": OCI_COMMIT_ID, "comment": comment})
    config["history"] = history
    config_bytes = canonical_json(config)
    config_digest = _digest(config_bytes)

    annotations = dict(parent_manifest.get("annotations") or {})
    annotations[PARENT_ANNOTATION] = parent_manifest_digest
    manifest = {
        "schemaVersion": 2,
        "mediaType": _MANIFEST,
        "config": {"mediaType": _CONFIG, "digest": config_digest, "size": len(config_bytes)},
        "layers": [*copy.deepcopy(layers), {"mediaType": _LAYER_GZIP, "digest": layer.digest, "size": layer.size}],
        "annotations": annotations,
    }
    manifest_bytes = canonical_json(manifest)
    return CommittedImage(config_bytes, config_digest, manifest_bytes, _digest(manifest_bytes))


def _file_digest(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _put_blob(blobs: Path, digest: str, source: Path | None = None, data: bytes | None = None) -> None:
    if not _DIGEST.fullmatch(digest):
        raise CommitError("commit-blob", f"invalid blob digest: {digest}")
    hex_digest = digest.split(":", 1)[1]
    target = blobs / hex_digest
    if target.exists():
        return
    if data is not None:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return
    assert source is not None
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
    fd = os.open(target, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_no_overwrite(src: Path, dst: Path) -> None:
    """Atomically move ``src`` directory to ``dst`` without overwriting ``dst`` if it exists."""
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"destination exists: {dst}")

    # Darwin: renamex_np with RENAME_EXCL
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if hasattr(libc, "renamex_np"):
                libc.renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
                libc.renamex_np.restype = ctypes.c_int
                # RENAME_EXCL = 0x00000004
                ret = libc.renamex_np(os.fsencode(src), os.fsencode(dst), 0x00000004)
                if ret != 0:
                    err = ctypes.get_errno()
                    if err in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR):
                        raise FileExistsError(f"destination exists: {dst}")
                    raise OSError(err, os.strerror(err), str(dst))
                return
        except (AttributeError, OSError) as e:
            if isinstance(e, (FileExistsError, OSError)) and not isinstance(e, AttributeError):
                raise
    # Linux: renameat2 with RENAME_NOREPLACE
    elif sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if hasattr(libc, "renameat2"):
                libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                libc.renameat2.restype = ctypes.c_int
                # AT_FDCWD = -100, RENAME_NOREPLACE = 1
                ret = libc.renameat2(-100, os.fsencode(src), -100, os.fsencode(dst), 1)
                if ret != 0:
                    err = ctypes.get_errno()
                    if err in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR):
                        raise FileExistsError(f"destination exists: {dst}")
                    raise OSError(err, os.strerror(err), str(dst))
                return
        except (AttributeError, OSError) as e:
            if isinstance(e, (FileExistsError, OSError)) and not isinstance(e, AttributeError):
                raise

    raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")


def write_commit_layout(
    dest: Path,
    *,
    parent_manifest: dict,
    parent_config: dict,
    parent_blob: Callable[[str], Path],
    layer_blob: Path,
    layer: GzipLayer,
    created: str,
    comment: str = "palimpsest commit",
    parent_manifest_digest: str | None = None,
) -> CommittedImage:
    """Write a new OCI layout directory: copies of the parent's layer blobs, the new layer,
    config, manifest and index. Atomically staged in a sibling directory.

    On any failure, the staging directory is cleaned up and the original ``layer_blob``
    and parent blobs remain completely unchanged.
    """
    if dest.exists() or dest.is_symlink():
        raise CommitError("commit-layout", "layout destination already exists")

    if not isinstance(parent_manifest_digest, str) or not _DIGEST.fullmatch(parent_manifest_digest):
        raise CommitError("commit-parent", "verified parent manifest digest is required")

    try:
        actual, size = _file_digest(layer_blob)
    except (FileNotFoundError, OSError) as err:
        raise CommitError("commit-layer", f"layer blob not found or unreadable: {layer_blob}") from err
    if (actual, size) != (layer.digest, layer.size):
        raise CommitError("commit-layer", "layer blob differs from its streamed identity")

    # Validates all parent layer descriptors and config before accessing any blobs
    image = build_committed_image(
        parent_manifest,
        parent_config,
        layer,
        created=created,
        comment=comment,
        parent_manifest_digest=parent_manifest_digest,
    )

    staging = dest.parent / f".{dest.name}.tmp-{uuid.uuid4().hex}"
    blobs = staging / "blobs" / "sha256"
    try:
        blobs.mkdir(parents=True, mode=0o700)
        os.chmod(staging, 0o700)
        for item in parent_manifest["layers"]:
            digest = item["digest"]
            try:
                source = parent_blob(digest)
                if not isinstance(source, Path):
                    source = Path(source)
            except Exception as err:
                raise CommitError("commit-parent", f"cannot resolve parent blob for {digest}: {err}") from err
            try:
                actual, size = _file_digest(source)
            except (FileNotFoundError, OSError) as err:
                raise CommitError("commit-parent", f"parent layer blob missing: {digest}") from err
            if (actual, size) != (digest, item["size"]):
                raise CommitError("commit-parent", "a parent layer blob does not match its descriptor")
            try:
                _put_blob(blobs, digest, source=source)
            except OSError as err:
                raise CommitError("commit-parent", f"failed writing parent blob {digest}: {err}") from err

        # Put new layer blob into staging without destroying original layer_blob
        try:
            _put_blob(blobs, layer.digest, source=layer_blob)
        except OSError as err:
            raise CommitError("commit-layer", f"failed writing layer blob {layer.digest}: {err}") from err
        _put_blob(blobs, image.config_digest, data=image.config)
        _put_blob(blobs, image.manifest_digest, data=image.manifest)
        descriptor = {"mediaType": _MANIFEST, "digest": image.manifest_digest, "size": len(image.manifest)}
        platform = {k: parent_config[k] for k in ("os", "architecture") if isinstance(parent_config.get(k), str)}
        if platform:
            descriptor["platform"] = platform
        index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [descriptor]}
        _write_file(staging / "index.json", canonical_json(index))
        _write_file(staging / "oci-layout", canonical_json({"imageLayoutVersion": "1.0.0"}))
        _fsync_dir(blobs)
        _fsync_dir(staging)

        if dest.exists() or dest.is_symlink():
            raise CommitError("commit-layout", "layout destination already exists")
        try:
            _rename_no_overwrite(staging, dest)
        except (FileExistsError, OSError) as err:
            raise CommitError(
                "commit-layout", f"layout destination already exists or appeared concurrently: {dest}"
            ) from err
        _fsync_dir(dest.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return image


def admit_capture(*, instance_state: str, source_hold: bool) -> None:
    """Fail closed unless the source instance is exactly stopped and unheld.

    Capture reads the upper of a stopped instance only; a live upper cannot be
    read consistently, and no stop is forced here. The caller stops the VM
    through the product path first and the source is never mutated.
    """
    if instance_state != "stopped":
        raise CommitError(
            "instance-not-stopped",
            f"commit requires a stopped instance (state {instance_state!r}); stop it first, nothing was changed",
        )
    if source_hold:
        raise CommitError("source-hold", "the instance has an unresolved hold; nothing was changed")


__all__ = [
    "OCI_COMMIT_ID",
    "PARENT_ANNOTATION",
    "CommitError",
    "CommittedImage",
    "admit_capture",
    "GzipLayer",
    "GzipTee",
    "build_committed_image",
    "canonical_json",
    "gzip_layer",
    "write_commit_layout",
]
