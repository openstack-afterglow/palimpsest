"""Consistent, source-preserving access to a stopped VZ instance's upper disk.

The upper ``datadisk`` of a stopped instance is never opened for writing and never handed to
a guest. Capture works on a private copy-on-write clone created next to a fresh scratch
directory; on any failure only that clone (and the scratch directory this call created) is
removed. A full byte copy is never used as a fallback: when the filesystem cannot clone, the
capture is refused.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import resource
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from .ext4_upper import (
    DEFAULT_UPPER,
    is_journal_dirty,
    limited_run,
    make_payload_opener,
    read_binding,
    read_upper,
    stage_payloads,
)
from .oci_capture import CaptureReceipt, convert_upper_to_layer
from .oci_commit import CommitError, CommittedImage, GzipTee, admit_capture, write_commit_layout

_FICLONE = 0x40049409
CLONE_NAME = "upper.ext4"


def clonefile(source: Path, destination: Path) -> None:
    """Copy-on-write clone ``source`` to the not-yet-existing ``destination`` or raise OSError."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if libc.clonefile(os.fsencode(source), os.fsencode(destination), 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return
    if sys.platform.startswith("linux"):
        source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            try:
                fcntl.ioctl(destination_fd, _FICLONE, source_fd)
            except OSError:
                os.close(destination_fd)
                destination_fd = -1
                os.unlink(destination)
                raise
            finally:
                if destination_fd >= 0:
                    os.close(destination_fd)
        finally:
            os.close(source_fd)
        return
    raise OSError(errno.ENOTSUP, "copy-on-write clone is unsupported on this platform")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def private_upper_clone(
    source: Path,
    scratch_parent: Path,
    *,
    expected_size: int,
    clone: Callable[[Path, Path], None] = clonefile,
) -> Iterator[Path]:
    """Yield a private clone of ``source``; remove exactly what this call created on exit."""
    source = Path(source)
    scratch_parent = Path(scratch_parent)
    before = os.lstat(source)
    if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
        raise CommitError("source-identity", "upper disk is not the expected regular file")
    scratch = scratch_parent / f"capture-{os.getpid()}-{os.urandom(8).hex()}"
    try:
        os.mkdir(scratch, 0o700)
    except OSError as exc:
        raise CommitError("scratch", f"could not create a private scratch directory: {exc.strerror}") from None
    target = scratch / CLONE_NAME
    try:
        try:
            clone(source, target)
        except OSError as exc:
            raise CommitError(
                "clone-unsupported", f"copy-on-write clone failed ({exc.strerror}); capture refused, nothing copied"
            ) from None
        after = os.lstat(source)
        if _identity(after) != _identity(before):
            raise CommitError("source-changed", "the upper disk changed while it was being cloned")
        copied = os.lstat(target)
        if not stat.S_ISREG(copied.st_mode) or copied.st_size != expected_size:
            raise CommitError("clone-identity", "clone is not the expected regular file")
        yield target
        if _identity(os.lstat(source)) != _identity(before):
            raise CommitError("source-changed", "the upper disk changed during capture")
    finally:
        shutil.rmtree(scratch, ignore_errors=False)


_BINDING_KEYS = ("run_id", "root_volume_id", "root_graph_digest", "root_nonce")


def verify_binding(actual: dict, expected: dict) -> None:
    """The disk must be this run's initialized upper, never another run's or an unbooted one."""
    if set(expected) != set(_BINDING_KEYS):
        raise CommitError("binding-expected", "the expected binding must name run, volume, graph and nonce")
    if actual.get("phase") != "initialized":
        raise CommitError("binding-phase", "the instance never booted far enough to have an upper; nothing to capture")
    for key in _BINDING_KEYS:
        if actual.get(key) != expected[key]:
            raise CommitError("binding-mismatch", f"the upper disk does not belong to this run ({key} differs)")


def _recover_journal(e2fsck: Path, image: Path) -> None:
    """Replay the journal on the private clone only (never the source)."""

    def limit() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    try:
        done = subprocess.run(
            [str(e2fsck), "-p", str(image)], capture_output=True, timeout=900, check=False, preexec_fn=limit
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommitError("journal-recovery", f"e2fsck could not run: {type(exc).__name__}") from None
    if done.returncode not in (0, 1):
        raise CommitError("journal-recovery", f"e2fsck could not repair the clone (exit {done.returncode})")


def capture_to_layout(
    *,
    disk: Path,
    disk_size: int,
    instance_state: str,
    source_hold: bool,
    scratch_parent: Path,
    debugfs: Path,
    destination: Path,
    parent_manifest: dict,
    parent_config: dict,
    parent_blob: Callable[[str], Path],
    parent_manifest_digest: str,
    created: str,
    upper: str = DEFAULT_UPPER,
    clone: Callable[[Path, Path], None] = clonefile,
    capture_options: dict | None = None,
    e2fsck: Path | None = None,
    expected_binding: dict,
) -> tuple[CommittedImage, CaptureReceipt]:
    """Admit, clone, read the upper, convert it to one layer and publish a new OCI layout.

    Refuses anything but an exactly stopped, unheld instance before touching the disk. The
    original disk is only ever cloned. ``destination`` is published by no-overwrite rename, so
    a failed capture leaves no layout behind and the source untouched.
    """
    admit_capture(instance_state=instance_state, source_hold=source_hold)
    with private_upper_clone(disk, scratch_parent, expected_size=disk_size, clone=clone) as image:
        if is_journal_dirty(image, debugfs, runner=limited_run):
            if e2fsck is None:
                raise CommitError(
                    "journal-dirty",
                    "the instance was not cleanly shut down (journal needs recovery); start and stop it again, "
                    "or allow recovery on the private clone. Nothing was changed",
                )
            _recover_journal(e2fsck, image)
            if is_journal_dirty(image, debugfs, runner=limited_run):
                raise CommitError("journal-recovery", "the journal still needs recovery after repair")
        verify_binding(read_binding(image, debugfs), expected_binding)
        entries = read_upper(image, debugfs, upper=upper)
        payloads = image.parent / "payloads"
        os.mkdir(payloads, 0o700)
        entries = stage_payloads(entries, debugfs, payloads)
        layer_path = image.parent / "layer.tar.gz"
        with layer_path.open("wb") as handle:
            tee = GzipTee(handle)
            receipt = convert_upper_to_layer(entries, tee, make_payload_opener(debugfs), **(capture_options or {}))
            layer = tee.finish()
        committed = write_commit_layout(
            destination,
            parent_manifest=parent_manifest,
            parent_config=parent_config,
            parent_blob=parent_blob,
            layer_blob=layer_path,
            layer=layer,
            created=created,
            parent_manifest_digest=parent_manifest_digest,
        )
    return committed, receipt


_DIGEST_PREFIX = "sha256:"


def _stage_parent_layers(
    manifest: dict, layer_streams: Mapping[str, Callable[[], AbstractContextManager[Iterable[bytes]]]], directory: Path
) -> dict[str, Path]:
    """Copy each parent layer blob into ``directory`` and verify its digest and size."""
    staged: dict[str, Path] = {}
    for item in manifest.get("layers", ()):
        digest, size = item.get("digest"), item.get("size")
        if digest in staged:
            continue
        opener = layer_streams.get(digest)
        if opener is None or not isinstance(digest, str) or not digest.startswith(_DIGEST_PREFIX):
            raise CommitError("commit-parent", "a parent layer is not available from the verified source")
        path = directory / digest.removeprefix(_DIGEST_PREFIX)
        hasher, total = hashlib.sha256(), 0
        with opener() as chunks, path.open("xb") as handle:
            for chunk in chunks:
                hasher.update(chunk)
                total += len(chunk)
                handle.write(chunk)
        if _DIGEST_PREFIX + hasher.hexdigest() != digest or total != size:
            raise CommitError("commit-parent", "a parent layer does not match its manifest digest or size")
        staged[digest] = path
    return staged


MAX_PARENT_PATHS = 5_000_000


def parent_layer_paths(manifest: dict, blobs: Mapping[str, Path]) -> set[str]:
    """Paths visible in the parent image after applying its layers in order (whiteouts honoured)."""
    visible: set[str] = set()
    for item in manifest.get("layers", ()):
        try:
            archive = tarfile.open(blobs[item["digest"]], mode="r|gz")
        except (OSError, tarfile.TarError, KeyError):
            raise CommitError("commit-parent", "a parent layer could not be read to list its paths") from None
        with archive:
            try:
                for member in archive:
                    name = os.path.normpath(member.name).lstrip("/")
                    if name in (".", ""):
                        continue
                    directory, _, base = name.rpartition("/")
                    if base == ".wh..wh..opq":
                        prefix = directory + "/" if directory else ""
                        visible = {path for path in visible if not path.startswith(prefix)} if prefix else set()
                    elif base.startswith(".wh."):
                        gone = (directory + "/" if directory else "") + base.removeprefix(".wh.")
                        visible = {path for path in visible if path != gone and not path.startswith(gone + "/")}
                    else:
                        visible.add(name)
                    if len(visible) > MAX_PARENT_PATHS:
                        raise CommitError("commit-parent", "the parent image has too many paths to inspect")
            except tarfile.TarError:
                raise CommitError("commit-parent", "a parent layer is not a readable tar") from None
    return visible


def commit_stopped_disk(
    *,
    disk: Path,
    disk_size: int,
    instance_state: str,
    source_hold: bool,
    expected_binding: dict,
    scratch_parent: Path,
    debugfs: Path,
    destination: Path,
    parent_manifest_bytes: bytes,
    parent_config_bytes: bytes,
    parent_manifest_digest: str,
    base_image_digest: str,
    layer_streams: Mapping[str, Callable[[], AbstractContextManager[Iterable[bytes]]]],
    created: str,
    generated_placeholders: Iterable[str] = (),
    injected_placeholders: Iterable[str] = (),
    capture_options: dict | None = None,
    e2fsck: Path | None = None,
    clone: Callable[[Path, Path], None] = clonefile,
) -> dict:
    """Capture a verified stopped disk into a new OCI layout and return the commit receipt.

    The parent manifest and config bytes are re-hashed here: the manifest must match
    ``parent_manifest_digest`` and the config must match the digest the manifest names, so a
    swapped source cannot become the parent. Parent layer blobs are streamed from the verified
    source, digest- and size-checked, and staged in a private scratch directory removed on exit.
    """
    admit_capture(instance_state=instance_state, source_hold=source_hold)
    if _DIGEST_PREFIX + hashlib.sha256(parent_manifest_bytes).hexdigest() != parent_manifest_digest:
        raise CommitError("commit-parent", "the parent manifest does not match its recorded digest")
    try:
        manifest = json.loads(parent_manifest_bytes)
        config = json.loads(parent_config_bytes)
    except ValueError:
        raise CommitError("commit-parent", "the parent manifest or config is not valid JSON") from None
    if not isinstance(manifest, dict) or not isinstance(config, dict):
        raise CommitError("commit-parent", "the parent manifest or config is malformed")
    named = (manifest.get("config") or {}).get("digest")
    if _DIGEST_PREFIX + hashlib.sha256(parent_config_bytes).hexdigest() != named:
        raise CommitError("commit-parent", "the parent config does not match the manifest")
    scratch_parent = Path(scratch_parent)
    stage = scratch_parent / f"parent-{os.getpid()}-{os.urandom(8).hex()}"
    try:
        os.mkdir(stage, 0o700)
    except OSError as exc:
        raise CommitError("scratch", f"could not create a private scratch directory: {exc.strerror}") from None
    try:
        blobs = _stage_parent_layers(manifest, layer_streams, stage)
        options = dict(capture_options or {})
        options.setdefault("generated_placeholders", tuple(generated_placeholders))
        options.setdefault("injected_placeholders", tuple(injected_placeholders))
        options.setdefault("parent_paths", parent_layer_paths(manifest, blobs))
        committed, _receipt = capture_to_layout(
            disk=disk,
            disk_size=disk_size,
            instance_state=instance_state,
            source_hold=source_hold,
            scratch_parent=scratch_parent,
            debugfs=debugfs,
            destination=destination,
            parent_manifest=manifest,
            parent_config=config,
            parent_blob=lambda digest: blobs[digest],
            parent_manifest_digest=parent_manifest_digest,
            created=created,
            clone=clone,
            capture_options=options,
            e2fsck=e2fsck,
            expected_binding=expected_binding,
        )
    finally:
        shutil.rmtree(stage, ignore_errors=False)
    new_manifest = json.loads(committed.manifest)
    return {
        "digest": committed.manifest_digest,
        "size_bytes": int(new_manifest["layers"][-1]["size"]),
        "parent_digest": parent_manifest_digest,
        "base_image_digest": base_image_digest,
    }


__all__ = [
    "CLONE_NAME",
    "capture_to_layout",
    "clonefile",
    "commit_stopped_disk",
    "parent_layer_paths",
    "private_upper_clone",
    "verify_binding",
]
