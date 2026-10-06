#!/usr/bin/env python3
"""Package a pinned Afterglow commit plus an explicit native-only overlay list."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

NATIVE_FILES = {"Dockerfile", "backend/scripts/native_vm.py", "scripts/export_native_cloud.py"}
NATIVE_DIRECTORY = "backend/scripts/native-cloud/"


def excluded(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (
        any(part == ".git" or part.startswith(".env") for part in parts)
        or PurePosixPath(name).name == "afterglow.conf"
        or name == "backend/tests/integration/credentials.toml"
    )


def validate_name(name: str) -> str:
    if name.startswith("/") or "\\" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    normalized = posixpath.normpath(name)
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise ValueError(f"Unsafe archive path: {name}")
    return normalized


def open_overlay(repository: Path, name: str):
    normalized = validate_name(name)
    if normalized != name or excluded(name):
        raise ValueError(f"Invalid native overlay: {name}")
    if name not in NATIVE_FILES and not name.startswith(NATIVE_DIRECTORY):
        raise ValueError(f"Overlay outside native allowlist: {name}")
    directory_fd = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    file_fd = None
    try:
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"Native overlay must be a singly-linked regular file: {name}")
        stream = os.fdopen(file_fd, "rb")
        file_fd = None
        return stream
    except OSError as exc:
        raise ValueError(f"Symlink or unavailable native overlay: {name}") from exc
    finally:
        os.close(directory_fd)
        if file_fd is not None:
            os.close(file_fd)


def file_identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class DigestReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def package(repository: Path, ref: str, overlays: list[str], destination: Path) -> dict:
    repository = repository.resolve(strict=True)
    if re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", ref) is None:
        raise ValueError("Use a full immutable commit SHA, not a branch, tag or HEAD")
    if len(set(overlays)) != len(overlays):
        raise ValueError("Duplicate native overlays")
    overlay_names = sorted(overlays)
    commit = subprocess.check_output(
        ["git", "--no-replace-objects", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repository, text=True
    ).strip()
    if commit != ref.lower():
        raise ValueError("Approved SHA must identify the commit itself")
    destination = destination.absolute()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sidecar = Path(str(destination) + ".manifest.json")
    if sidecar.exists() or sidecar.is_symlink():
        raise ValueError("Refusing to overwrite an existing source manifest")
    files = []
    omissions = []
    temporary = None
    process = None
    manifest_temporary = None
    overlay_streams = {}
    try:
        for name in overlay_names:
            stream = open_overlay(repository, name)
            overlay_streams[name] = (stream, os.fstat(stream.fileno()))
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as raw:
            temporary = Path(raw.name)
            os.chmod(temporary, 0o600)
            with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as output:
                    process = subprocess.Popen(
                        ["git", "--no-replace-objects", "archive", "--format=tar", commit],
                        cwd=repository,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    assert process.stdout is not None
                    with tarfile.open(fileobj=process.stdout, mode="r|") as baseline:
                        for original in baseline:
                            name = validate_name(original.name)
                            if excluded(name):
                                omissions.append(name)
                                continue
                            if name in overlay_streams:
                                continue
                            if not (original.isdir() or original.isfile() or original.issym()):
                                raise ValueError(f"Unsupported baseline entry: {name}")
                            if original.issym():
                                target = original.linkname
                                if target.startswith("/") or "\\" in target:
                                    raise ValueError(f"Escaping baseline symlink: {name}")
                                validate_name(posixpath.join(posixpath.dirname(name), target))
                            original.name = name
                            original.uid = original.gid = original.mtime = 0
                            original.uname = original.gname = ""
                            original.pax_headers = {}
                            if original.isfile():
                                stream = baseline.extractfile(original)
                                assert stream is not None
                                reader = DigestReader(stream)
                                output.addfile(original, reader)
                                files.append({"path": name, "sha256": reader.digest.hexdigest(), "origin": "commit"})
                            else:
                                output.addfile(original)
                                if original.issym():
                                    files.append({"path": name, "symlink": original.linkname, "origin": "commit"})
                    stderr = process.stderr.read() if process.stderr is not None else b""
                    if process.wait() != 0:
                        raise RuntimeError(f"git archive failed: {stderr.decode(errors='replace')}")
                    for name, (stream, metadata) in overlay_streams.items():
                        info = tarfile.TarInfo(name)
                        info.size = metadata.st_size
                        info.mode = 0o755 if metadata.st_mode & 0o111 else 0o644
                        reader = DigestReader(stream)
                        output.addfile(info, reader)
                        if file_identity(os.fstat(stream.fileno())) != file_identity(metadata):
                            raise RuntimeError(f"Overlay changed while packaging: {name}")
                        files.append({"path": name, "sha256": reader.digest.hexdigest(), "origin": "native-overlay"})
            raw.flush()
            os.fsync(raw.fileno())
        with temporary.open("rb") as stream:
            bundle_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {
            "schema_version": 1,
            "afterglow_ref": commit,
            "bundle_sha256": bundle_sha256,
            "bundle_bytes": temporary.stat().st_size,
            "overlays": overlay_names,
            "excluded": sorted(set(omissions)),
            "explicit_exclusions": [
                ".git",
                ".env* at any depth",
                "afterglow.conf",
                "backend/tests/integration/credentials.toml",
            ],
            "files": sorted(files, key=lambda item: item["path"]),
        }
        # The atomic manifest is the commit marker. A bundle alone is incomplete.
        with tempfile.NamedTemporaryFile(dir=destination.parent, mode="w", encoding="utf-8", delete=False) as stream:
            manifest_temporary = Path(stream.name)
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            try:
                os.link(temporary, destination)
            except FileExistsError:
                # Recover only an identical private bundle, never replace another output.
                descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as existing:
                    before = os.fstat(existing.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_mode & 0o077:
                        raise ValueError("Existing bundle is not an owner-private regular file") from None
                    digest = hashlib.file_digest(existing, "sha256").hexdigest()
                    visible = destination.lstat()
                    if (
                        digest != bundle_sha256
                        or file_identity(os.fstat(existing.fileno())) != file_identity(before)
                        or (visible.st_dev, visible.st_ino) != (before.st_dev, before.st_ino)
                    ):
                        raise ValueError("Existing incomplete bundle differs from approved inputs") from None
            os.link(manifest_temporary, sidecar)
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            manifest_temporary.unlink(missing_ok=True)
        return manifest
    finally:
        for stream, _metadata in overlay_streams.values():
            stream.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if manifest_temporary is not None:
            manifest_temporary.unlink(missing_ok=True)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--ref", required=True, help="Exact authorized baseline commit")
    parser.add_argument("--overlay", action="append", default=[], help="Explicit reviewed native path; repeat")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = package(args.repository, args.ref, args.overlay, args.output)
    print(json.dumps({key: manifest[key] for key in ("afterglow_ref", "bundle_sha256", "bundle_bytes", "overlays")}))


if __name__ == "__main__":
    main()
