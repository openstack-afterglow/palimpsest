#!/usr/bin/env python3
"""Build and smoke-test local Palimpsest distribution artifacts."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET = "palimpsest_local/assets/oci-stage1-init.x86_64"
SOURCE_ASSET = ROOT / "src" / ASSET


def _run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _source_date_epoch() -> str:
    configured = os.environ.get("SOURCE_DATE_EPOCH")
    if configured is not None:
        if not configured.isascii() or not configured.isdecimal():
            raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer")
        return configured
    result = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, check=True, capture_output=True, text=True)
    value = result.stdout.strip()
    if not value.isascii() or not value.isdecimal():
        raise RuntimeError("Git did not return a valid commit timestamp")
    return value


def _one(directory: Path, pattern: str, label: str) -> Path:
    found = tuple(directory.glob(pattern))
    if len(found) != 1:
        raise RuntimeError(f"build must produce exactly one {label}")
    return found[0]


def _validate_sdist(sdist: Path, expected_asset: bytes) -> None:
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        assets = [member for member in members if member.name.endswith(f"/src/{ASSET}")]
        if len(assets) != 1:
            raise RuntimeError("source distribution must contain exactly one stage-1 ELF asset")
        stream = archive.extractfile(assets[0])
        if stream is None or stream.read() != expected_asset:
            raise RuntimeError("source distribution stage-1 ELF does not match the source asset")
        for member in members:
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError("source distribution contains an unreadable file")
                while stream.read(1024 * 1024):
                    pass


def _validate_wheel(wheel: Path, expected_asset: bytes) -> None:
    with zipfile.ZipFile(wheel) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("wheel contains a corrupt member")
        if archive.namelist().count(ASSET) != 1 or archive.read(ASSET) != expected_asset:
            raise RuntimeError("wheel stage-1 ELF does not match the source asset")


def _verify_install(wheel: Path, workspace: Path, uv: str, version: str) -> None:
    environment = workspace / "venv"
    _run([uv, "venv", "--python", sys.executable, str(environment)], cwd=workspace)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [uv, "pip", "install", "--python", str(python), "--no-deps", "--no-index", str(wheel)],
        cwd=workspace,
    )
    probe = (
        "import importlib.metadata, pathlib, sys; import palimpsest_local; "
        f"assert importlib.metadata.version('palimpsest-local') == {version!r}; "
        "assert pathlib.Path(palimpsest_local.__file__).is_relative_to(pathlib.Path(sys.prefix)); "
        "from palimpsest_local.oci_initramfs import _bootstrap_stage1_binary; "
        "_bootstrap_stage1_binary()"
    )
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    _run([str(python), "-I", "-c", probe], env=clean_environment, cwd=workspace)

    tool_environment = clean_environment.copy()
    tool_environment["UV_TOOL_DIR"] = str(workspace / "tool-envs")
    tool_environment["UV_TOOL_BIN_DIR"] = str(workspace / "tool-bin")
    _run(
        [uv, "tool", "install", "--python", str(python), "--no-index", str(wheel)],
        env=tool_environment,
        cwd=workspace,
    )
    executable = workspace / "tool-bin" / ("palimpsest.exe" if os.name == "nt" else "palimpsest")
    _run([str(executable), "--version"], env=tool_environment, cwd=workspace)
    _run([str(executable), "--help"], env=tool_environment, cwd=workspace)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_output(out_dir: Path) -> Path:
    if out_dir.is_symlink() or out_dir.exists():
        raise RuntimeError("output directory already exists; choose a new path")
    out_dir = out_dir.resolve(strict=False)
    out_dir.mkdir(parents=True)
    return out_dir


def build(out_dir: Path, uv: str = "uv") -> tuple[Path, Path, Path]:
    out_dir = _prepare_output(out_dir)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    expected_asset = SOURCE_ASSET.read_bytes()
    with tempfile.TemporaryDirectory(prefix="palimpsest-package-") as temporary:
        workspace = Path(temporary)
        sdist_dir = workspace / "sdist"
        wheel_dir = workspace / "wheel"
        environment = os.environ.copy()
        environment["SOURCE_DATE_EPOCH"] = _source_date_epoch()
        _run([uv, "build", "--sdist", "--out-dir", str(sdist_dir)], env=environment)
        sdist = _one(sdist_dir, "*.tar.gz", "source distribution")
        _validate_sdist(sdist, expected_asset)
        _run([uv, "build", "--wheel", str(sdist), "--out-dir", str(wheel_dir)], env=environment)
        wheel = _one(wheel_dir, "*.whl", "wheel")
        _validate_wheel(wheel, expected_asset)
        _verify_install(wheel, workspace, uv, version)

        published = tuple(out_dir / artifact.name for artifact in (wheel, sdist))
        for source, destination in zip((wheel, sdist), published, strict=True):
            with source.open("rb") as input_stream, destination.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
        checksums = out_dir / "SHA256SUMS"
        with checksums.open("x", encoding="ascii") as stream:
            stream.write("".join(f"{_digest(path)}  {path.name}\n" for path in sorted(published)))
    return published[0], published[1], checksums


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--uv", default="uv", help="uv executable name or absolute path")
    arguments = parser.parse_args(argv)
    try:
        artifacts = build(arguments.out_dir, arguments.uv)
    except (KeyError, OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        parser.exit(1, f"package build failed: {exc}\n")
    for artifact in artifacts:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
